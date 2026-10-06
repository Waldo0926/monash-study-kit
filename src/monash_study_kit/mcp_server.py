"""本地 MCP 服务器（stdio）：让 Claude Desktop / Claude Code 直接查 Moodle 和 Ed。

    monash mcp          # 由 Claude 启动，不用自己跑；monash setup 会帮你把它加进 Claude

只读：不交作业、不做测验、不发帖。唯一会“动”的是 monash_login（在你电脑上打开登录窗口）
和 monash_sync（在后台同步）。

服务器开着的时候（也就是 Claude 开着的时候）在后台做两件事：
  * 每 20 分钟给 Moodle 会话续一次期——Moodle 空闲 4 小时就把你踢出去；
  * 每小时同步一次 Ed 和 Moodle，再更新全文索引。
后台的输出写进 mcp.log，绝不能写 stdout（stdout 是给 Claude 的协议通道）。

MCP 本身不消耗额度，只有工具返回的内容进上下文才算，所以每个工具的返回都有长度上限。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
import zipfile

from . import __version__, edlib, edquery, help_menu, jobs, textextract
from . import features as F
from .edlib import EdAuthError
from .moodlelib import COOKIE_FILE, FILES_DIR, MoodleAuthError, MoodleClient, db_connect
from .paths import ED_FILES_DIR, HOME, LOG_FILE, ensure_private_dir, load_settings
from .syncer import course_code, course_folder

PROTOCOL = "2025-06-18"
MAX_TEXT = 20000
MAX_OUT = 60000
ED_CURSOR = "mcp"      # 和命令行的游标（cli）分开记
TEXT_EXT = {".txt", ".md", ".py", ".hs", ".ts", ".js", ".java", ".c", ".cpp", ".h", ".sh", ".sql", ".csv",
            ".json", ".mzn", ".dzn", ".r", ".rmd", ".html", ".css", ".yaml", ".yml", ".toml", ".ipynb"}

INSTRUCTIONS = (
    "Monash 学习助手（Moodle + Ed，只读，不能交作业、做测验、发帖）。“这周要做什么”先用 study_todo；"
    "知识点在哪份资料用 search_content 再 read_file；Ed 新消息用 ed_updates。数据每小时后台同步。"
    "报“Moodle 登录已过期”时问用户要不要登录，同意就调 monash_login；Ed 令牌失效请用户在终端运行 "
    "monash login ed（令牌别贴进聊天）。用户问“你能做什么/help”时，按类别列出功能并各给一句示例问法，"
    "并告诉他输入框“+”菜单里有 monash 的预设提示。")


# ---------------------------------------------------------------- 日志

def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- Moodle 客户端

_client: MoodleClient | None = None
_client_stamp: float | None = None


def client() -> MoodleClient:
    """重新登录后 cookie 文件会变，这时换一个新客户端，别拿着旧会话一直报过期。"""
    global _client, _client_stamp
    try:
        stamp = COOKIE_FILE.stat().st_mtime
    except OSError:
        stamp = None
    if _client is None or stamp != _client_stamp:
        _client = MoodleClient()
        _client_stamp = stamp
    return _client


def _client_or_none() -> MoodleClient | None:
    try:
        return client()
    except MoodleAuthError:
        return None


def _courses() -> list[dict]:
    return client().courses()


def _course(ref: str) -> dict:
    return F.find_course(_courses(), ref)


def _tracked_or_current() -> list[dict]:
    """跟踪的课；从没选过就是本学期的课（和 monash sync 用的同一套规则）。"""
    con = db_connect()
    return jobs.tracked_courses(con, jobs.load_courses(client(), con))


def _clip(text: str, limit: int, offset: int = 0) -> str:
    part = text[offset:offset + limit]
    if offset + limit < len(text):
        part += f"\n\n[还有 {len(text) - offset - limit} 字符未显示，用 offset={offset + limit} 继续读]"
    return part


# ---------------------------------------------------------------- 后台：登录、同步、续期

_login = {"running": False, "result": None, "error": None}
_sync_thread: threading.Thread | None = None


def _run_login():
    from .browser_login import LoginFailed, ensure_login
    try:
        r = ensure_login(log=log)
        _login.update(result=r, error=None)
        log(f"登录成功，会话还剩 {r.get('time_remaining')} 秒")
        start_sync()
    except (LoginFailed, OSError) as e:
        _login.update(error=str(e))
        log(f"登录失败：{e}")
    finally:
        _login["running"] = False


def start_sync() -> bool:
    global _sync_thread
    if _sync_thread and _sync_thread.is_alive():
        return False
    _sync_thread = threading.Thread(target=_safe_sync, name="sync", daemon=True)
    _sync_thread.start()
    return True


def _safe_sync():
    try:
        st = jobs.sync_all(log=log)
        log(f"同步完成：{json.dumps(st.get('errors') or {}, ensure_ascii=False)}")
    except Exception:  # noqa: BLE001
        log("同步出错：\n" + traceback.format_exc())


def _update_hint() -> str:
    from . import update_check
    msg = update_check.notice()
    return f"（{msg}，合适的时候提醒用户一次。）" if msg else ""


def _background():
    """Claude 开着的时候：按设置定时同步、给 Moodle 续期。"""
    settings = load_settings()
    every_sync = max(0.25, float(settings.get("auto_sync_hours") or 1)) * 3600
    every_touch = max(5, int(settings.get("keepalive_minutes") or 20)) * 60
    time.sleep(5)       # 先让 Claude 把握手做完
    last_touch = 0.0
    while True:
        from . import update_check
        if update_check.enabled():
            update_check.refresh()          # 自己控制频率：一天最多真查一次
        if jobs.hours_since_sync() * 3600 >= every_sync:
            start_sync()
        if time.time() - last_touch >= every_touch:
            last_touch = time.time()
            try:
                jobs.keepalive()
            except MoodleAuthError:
                pass      # 没登录或已过期：等用户登录，不自己弹窗
            except Exception as e:  # noqa: BLE001
                log(f"续期失败：{e}")
        time.sleep(60)


# ---------------------------------------------------------------- 管理工具

def t_status(args):
    if args.get("full"):
        from . import doctor
        # Claude Code 那项要起一个 claude 子进程，最多 20 秒，在这里没必要
        return doctor.run(skip=("check_claude_code",))
    st = jobs.read_status()
    moodle = {"logged_in": False}
    try:
        c = client()
        left = c.time_remaining()
        moodle = {"logged_in": True, "time_remaining_min": (left or 0) // 60}
    except MoodleAuthError as e:
        moodle["problem"] = str(e)
        moodle["calendar_fallback"] = bool(F.load_calendar_url())
    ed = {"token_set": jobs.have_ed_token()}
    try:
        con = edlib.db_connect()
        ed["tracked_courses"] = [edquery._short_code(r["code"]) for r in
                                 con.execute("SELECT code FROM courses WHERE tracked=1")]
        con.close()
    except Exception:  # noqa: BLE001
        pass
    from . import update_check
    return {"moodle": moodle, "ed": ed, "last_sync": st.get("last_sync"), "sync_errors": st.get("errors"),
            "update_available": update_check.available(),
            "syncing": bool(_sync_thread and _sync_thread.is_alive()),
            "login_window": {"running": _login["running"], "last_error": _login["error"]},
            "files_dir": str(FILES_DIR), "version": __version__}


def t_login(args):
    from .browser_login import session_ok
    if _login["running"]:
        return "登录窗口已经开着了，请用户在窗口里完成登录。"
    if session_ok():
        return "Moodle 已经是登录状态，不用重新登录。"
    _login.update(running=True, error=None)
    threading.Thread(target=_run_login, name="login", daemon=True).start()
    return ("正在用户电脑上打开 Moodle 登录窗口（如果 Okta 还记得登录，会在后台自动完成、不弹窗）。"
            "请用户在窗口里登录 Monash，完成后窗口会自动关闭，并开始同步。"
            "告诉用户登录好之后说一声，再调用 monash_status 确认。")


def t_sync(args):
    if start_sync():
        return "已在后台开始同步（第一次可能要几分钟）。之后用 monash_status 看结果。"
    return "已经在同步了，稍后用 monash_status 看结果。"


# ---------------------------------------------------------------- Moodle 工具

def t_todo(args):
    from . import todo
    c = _client_or_none()
    try:
        courses = _tracked_or_current() if c else []
    except MoodleAuthError:
        courses = []
    from . import update_check
    out = todo.build(c, courses, int(args.get("days", 7)))
    if (msg := update_check.notice()):
        out["notes"].append(msg)
    return out


def t_courses(args):
    """两边的课一起列。Moodle 没登录时用本地库里记下的课程表。"""
    try:
        moodle = [{"id": c["id"], "code": course_code(c), "name": course_folder(c)} for c in _courses()]
    except MoodleAuthError:
        con = db_connect()
        moodle = [{"id": r["id"], "code": r["code"], "name": r["folder"], "tracked": bool(r["tracked"])}
                  for r in con.execute("SELECT id, code, folder, tracked FROM courses ORDER BY id DESC")]
    return {"moodle": moodle, "ed": edquery.courses(_ed())}


def t_due(args):
    course = _course(args["course"]) if args.get("course") else None
    return [{k: d[k] for k in ("course", "activity", "name", "kind", "due", "overdue", "action", "url", "source")}
            for d in F.due(_client_or_none(), int(args.get("days", 14)), course)]


def t_assignments(args):
    cs = [_course(args["course"])] if args.get("course") else _tracked_or_current()
    items = [a for c in cs for a in F.assignments(client(), c)]
    return [a for a in items if a["missing"]] if args.get("missing_only") else items


def t_messages(args):
    if args.get("conversation_id"):
        return F.conversation_messages(client(), int(args["conversation_id"]), int(args.get("limit", 30)))
    return F.conversations(client(), int(args.get("limit", 20)))


def t_search_content(args):
    from . import content_index as CI
    course = args.get("course")
    code = None
    if course:
        m = re.search(r"[A-Za-z]{3}\d{4}", course)
        code = m.group(0).upper() if m else course_code(_course(course))
    return CI.search(db_connect(), args["query"], code, min(int(args.get("limit", 15)), 50))


def t_grades(args):
    if not args.get("course"):
        return F.grades_overview(client())
    return F.grades(client(), _course(args["course"])["id"], bool(args.get("graded_only")))


def t_forum(args):
    """不给 query：最近的课程公告（含正文）；给了：搜 Moodle 论坛。"""
    limit = int(args.get("limit", 5 if args.get("query") is None else 10))
    if args.get("query"):
        cid = _course(args["course"])["id"] if args.get("course") else 1
        res = F.forum_search(client(), args["query"], cid, limit)
        for r in res:
            r["text"] = r["text"][:800]
        return res
    courses = [_course(args["course"])] if args.get("course") else _tracked_or_current()
    out = []
    for co in courses:
        out += F.news(client(), co, min(limit, 5))
    out.sort(key=lambda x: -(x.get("time_ts") or 0))
    for n in out:
        n["text"] = n["text"][:1500]
        n.pop("time_ts", None)
    return out[:limit * 2]


def _local_course_id(ref: str) -> int:
    """list_files 只读本地库，会话过期时也能用：先按本地课程表找，找不到再问 Moodle。"""
    con = db_connect()
    ref_u = ref.strip().upper()
    for r in con.execute("SELECT id, code, fullname FROM courses ORDER BY id DESC"):
        if str(r["id"]) == ref.strip() or (r["code"] or "") == ref_u or ref.lower() in (r["fullname"] or "").lower():
            return r["id"]
    return _course(ref)["id"]


def t_list_files(args):
    con = db_connect()
    if args.get("links"):
        if not args.get("course"):
            raise ValueError("links=true 要同时给 course")
        q, params = "SELECT folder, section, title, url FROM links WHERE course_id=?", [_local_course_id(args["course"])]
        if args.get("week"):
            q += " AND folder LIKE ?"
            params.append(f"Week {int(args['week']):02d}%")
        return [dict(r) for r in con.execute(q + " ORDER BY folder, rowid LIMIT 200", params)]
    q = "SELECT f.path, f.size, f.title FROM files f WHERE 1=1"
    params = []
    if args.get("course"):
        q += " AND f.course_id=?"
        params.append(_local_course_id(args["course"]))
    if args.get("week"):
        q += " AND f.path LIKE ?"
        params.append(f"%/Week {int(args['week']):02d}%")
    if args.get("query"):
        q += " AND (f.path LIKE ? OR f.title LIKE ?)"
        params += [f"%{args['query']}%"] * 2
    rows = con.execute(q + " ORDER BY f.path LIMIT 200", params).fetchall()
    if not rows and not con.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        return {"files": [], "note": "还没同步过 Moodle 课件（没登录，或者第一次同步还没跑完；看 monash_status）"}
    return [{"path": r["path"], "bytes": r["size"], "activity": r["title"]} for r in rows]


def t_read_file(args):
    """读一个已同步的课件：文本直接给；PDF 按页抽文字；docx/pptx 抽文字；zip 先列清单。"""
    rel = args["path"].replace("\\", "/").lstrip("/")
    base = FILES_DIR
    if rel.startswith("ed:"):                  # search_content 给的 Ed 课件路径
        base, rel = ED_FILES_DIR, rel[3:].lstrip("/")
    path = (base / rel).resolve()
    if base.resolve() not in path.parents or not path.is_file():
        raise ValueError("只能读 list_files 或 search_content 给出的路径")
    limit = min(int(args.get("max_chars", MAX_TEXT)), 100000)
    offset = int(args.get("offset", 0))
    ext = path.suffix.lower()
    if ext in TEXT_EXT:
        text = path.read_text(encoding="utf-8", errors="replace")
        if ext == ".ipynb":
            nb = json.loads(text)
            text = "\n\n".join(("# [markdown]\n" if c.get("cell_type") == "markdown" else "# [code]\n")
                               + "".join(c.get("source", [])) for c in nb.get("cells", []))
        return _clip(text, limit, offset)
    if ext == ".pdf":
        pages = textextract.pdf_pages(path)
        if not any(p.strip() for p in pages):
            raise ValueError("这个 PDF 抽不出文字（可能是扫描件或纯图片）")
        return _clip("\n\n".join(f"--- 第 {i} 页 ---\n{p}" for i, p in enumerate(pages, 1)), limit, offset)
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            inner = args.get("inner")
            if not inner:
                return {"zip": rel, "files": names[:300]}
            if inner not in names:
                raise ValueError(f"zip 里没有 {inner}")
            return _clip(z.read(inner).decode(errors="replace"), limit, offset)
    if ext in (".docx", ".pptx"):
        parts = textextract.office_parts(path)
        return _clip("\n\n".join(f"[{loc}] {t}" if loc else t for loc, t in parts), limit, offset)
    raise ValueError(f"{ext} 这种文件读不了文字；文件在 {path}")


# ---------------------------------------------------------------- Ed 工具

def _ed():
    return edlib.db_connect()


def _slim(rows: list[dict]) -> list[dict]:
    keep = ("ref", "id", "course", "type", "title", "category", "subcategory", "author", "created_at",
            "reply_count", "is_pinned", "is_private", "is_answered", "is_staff_answered",
            "snippet", "match_in", "url", "is_starred", "is_watched", "is_mine", "new_reply_count")
    return [{k: r[k] for k in keep if r.get(k) not in (None, False, "")} for r in rows]


def _with_replies(t: dict) -> dict:
    return dict(_slim([t])[0], new_replies=[{**r, "text": (r["text"] or "")[:1200]} for r in t["new_replies"]])


def t_ed_updates(args):
    """上次调用之后的新帖/新回复 + 我的帖子（发的/关注的/收藏的）有没看过的回复。"""
    conn = _ed()
    d = edquery.new_activity(conn, args.get("since"), args.get("course"), cursor=ED_CURSOR,
                             advance=not args.get("peek", False), limit=int(args.get("limit", 30)))
    seen = {t["id"] for t in d["new_threads"]} | {t["id"] for t in d["threads_with_new_replies"]}
    mine = [t for t in edquery.following(conn, args.get("course"), 20)
            if (t["is_mine"] or t["is_watched"] or t["is_starred"]) and t["id"] not in seen]
    return {"since": d["since"], "last_sync": d["last_sync"], "new_threads": _slim(d["new_threads"]),
            "threads_with_new_replies": [_with_replies(t) for t in d["threads_with_new_replies"]],
            "my_threads_with_unread_replies": [_with_replies(t) for t in mine]}


# only= 的每个选项对应帖子上的哪个字段（和 edquery.STATE_FILTERS 的 SQL 一一对应，搜索结果在 Python 里过滤）
ONLY_MATCH = {"starred": lambda r: r.get("is_starred"), "watching": lambda r: r.get("is_watched"),
              "unseen": lambda r: not r.get("is_seen"), "mine": lambda r: r.get("is_mine"),
              "unread_replies": lambda r: (r.get("new_reply_count") or 0) > 0}


def t_ed_threads(args):
    only = args.get("only")
    if only and only not in ONLY_MATCH:
        raise ValueError(f"only 只能是 {'/'.join(edquery.STATE_FILTERS)}")
    limit = min(int(args.get("limit", 15)), 50)
    if args.get("query"):
        rows = edquery.search(_ed(), args["query"], args.get("course"), args.get("since"),
                              args.get("category"), args.get("type"), limit)
        return _slim([r for r in rows if not only or ONLY_MATCH[only](r)])
    rows = edquery.list_threads(_ed(), args.get("course"), args.get("since"), args.get("category"),
                                args.get("type"), bool(args.get("unanswered")), limit, int(args.get("offset", 0)),
                                [only] if only else [])
    return _slim(rows)


def t_ed_thread(args):
    from . import edsync
    conn = _ed()
    note = ""
    if args.get("live"):
        try:
            tid = edquery.resolve_thread(conn, args["thread"])
        except edquery.NotFound:
            if not str(args["thread"]).isdigit():
                raise
            tid = int(args["thread"])
        try:
            edsync.refresh_thread(conn, tid)
        except edlib.EdForbidden:
            note = "[注意：Ed 上已经看不到这个帖子了（被删除或改成私密），下面是库里的旧副本]\n\n"
        args = dict(args, thread=str(tid))
    t = edquery.get_thread(conn, args["thread"])
    return _clip(note + edquery.thread_markdown(t), min(int(args.get("max_chars", 30000)), 100000),
                 int(args.get("offset", 0)))


def t_ed_lessons(args):
    clip = lambda text: _clip(text, min(int(args.get("max_chars", 30000)), 100000), int(args.get("offset", 0)))  # noqa: E731
    if args.get("lesson"):
        from . import lesson_reader
        return clip(lesson_reader.lesson_markdown(_ed(), args["course"], args["lesson"])["markdown"])
    if args.get("quiz"):
        rows = edquery.quizzes(_ed(), args["course"], args.get("module"), args.get("status"))
        return _clip(edquery.quiz_markdown(rows), min(int(args.get("max_chars", 30000)), 100000),
                     int(args.get("offset", 0)))
    rows = edquery.lessons(_ed(), args["course"], args.get("module"), args.get("status"))
    for r in rows:
        for f in r["files"]:
            f.pop("local_path", None)
    return rows


# ---------------------------------------------------------------- 工具表
#
# 工具清单每次对话都要整份发给 Claude（不调用也算额度），所以：能合并的合并，说明写短，
# 只给容易误解的参数补短 schema 说明。改完跑 tests/test_mcp.py 里的体积测试。

S, I, B = "string", "integer", "boolean"
STATUS = ("completed", "attempted", "unattempted")

TOOLS = {
    "study_todo": (t_todo, "综合待办：截止、漏交、Lesson、公告、未读回复；问“这周要做/交什么”用。返回 due/possibly_missing/ed_lessons/announcements/ed_unread_replies",
                   {"days": (I, False)}),
    "monash_status": (t_status, "查登录、同步和错误；报错或“用不了”时用，full=true 完整体检。返回登录、同步、版本和错误状态。",
                      {"full": (B, False)}),
    "monash_login": (t_login, "仅在用户同意重新登录后用：打开本机 Moodle 登录窗口；已登录则不重复打开，成功后自动同步。结果/失败用 monash_status 确认。", {}, False),
    "monash_sync": (t_sync, "后台同步 Moodle/Ed 并更新索引；用户要求刷新或数据过旧时用，已在同步则不重复启动。结果/错误用 monash_status 查看。",
                    {}, False),
    "courses": (t_courses, "列 Moodle/Ed 课程；问有哪些课或需确定课程时用。Moodle 未登录时回退本地课程表；返回课程代码、名称和标识。", {}),
    "moodle_due": (t_due, "查未来截止/日历事件；作业详情用 moodle_assignments，成绩用 moodle_grades；未登录可用 iCal。返回课程、活动、截止时间、逾期状态和链接。",
                   {"course": (S, False), "days": (I, False)}),
    "moodle_assignments": (t_assignments, "查作业截止/提交/成绩；missing_only 仅漏交。近期截止用 moodle_due，成绩用 moodle_grades。返回作业、截止、提交和可用成绩状态。",
                           {"course": (S, False), "missing_only": (B, False)}),
    "moodle_grades": (t_grades, "查成绩；无 course 看总览，有则看评分项/得分/反馈。提交状态用 moodle_assignments。返回评分项、得分和可用反馈。",
                      {"course": (S, False), "graded_only": (B, False)}),
    "moodle_forum": (t_forum, "Moodle 公告/论坛：无 query 看公告，有则搜索；Ed 讨论用 ed_threads。返回公告或匹配帖子摘要、正文片段和链接。",
                     {"query": (S, False), "course": (S, False), "limit": (I, False)}),
    "moodle_messages": (t_messages, "Moodle 私信：无 conversation_id 列对话，有则读消息；不会标已读。返回对话列表或指定对话消息。",
                        {"conversation_id": (I, False), "limit": (I, False)}),
    "search_content": (t_search_content, "全文搜课件/PDF/笔记/字幕；只列文件用 list_files，命中 path 用 read_file。返回匹配片段、文件路径和定位信息。",
                       {"query": (S, True), "course": (S, False), "limit": (I, False)}),
    "list_files": (t_list_files, "列本地课件/外链；不搜正文。搜内容用 search_content，path 用 read_file。返回文件/外链及可供 read_file 使用的路径。",
                   {"course": (S, False), "week": (I, False), "query": (S, False), "links": (B, False)}),
    "read_file": (t_read_file, "读课件；path 来自 list_files/search_content。ZIP 用 inner，长内容用 offset/max_chars 分页。返回文件文本或 ZIP 文件清单。",
                  {"path": (S, True), "inner": (S, False), "offset": (I, False), "max_chars": (I, False)}),
    "ed_updates": (t_ed_updates, "查 Ed 新帖/回复和本人相关未读回复；浏览/搜索帖子用 ed_threads。返回 since/last_sync、新帖、新回复和本人相关更新。",
                   {"course": (S, False), "since": (S, False), "peek": (B, False), "limit": (I, False)}),
    "ed_threads": (t_ed_threads, "浏览/搜索 Ed 帖子；全文用 ed_thread，新动态用 ed_updates。返回 ref/title/author/time/replies/url 等摘要；浏览模式用 offset 分页。",
                   {"query": (S, False), "course": (S, False), "since": (S, False),
                    "type": (S, False, ("question", "post", "announcement")),
                    "unanswered": (B, False), "only": (S, False, tuple(edquery.STATE_FILTERS)),
                    "category": (S, False), "limit": (I, False), "offset": (I, False)}),
    "ed_thread": (t_ed_thread, "读单个 Ed 帖子全文/回复；找帖子用 ed_threads，新动态用 ed_updates；live=true 先刷新。返回帖子 Markdown，长内容用 offset/max_chars 分页。",
                  {"thread": (S, True), "live": (B, False), "offset": (I, False), "max_chars": (I, False)}),
    "ed_lessons": (t_ed_lessons, "查 Ed Lessons：列进度、读单节或测验。帖子用 ed_threads；跨课件搜内容用 search_content。返回 Lesson 列表/进度、指定 Lesson 正文或测验内容。",
                   {"course": (S, True), "module": (S, False), "lesson": (S, False),
                    "status": (S, False, STATUS), "quiz": (B, False),
                    "offset": (I, False), "max_chars": (I, False)}),
}

# 每个参数都给短 schema 说明：让 agent 不必猜参数语义，同时控制上下文开销。
PARAM_HELP = {
    ("study_todo", "days"): "未来天数，默认7",
    ("monash_status", "full"): "true=完整诊断；false=简要状态",
    ("moodle_due", "course"): "课程号，如 FIT2102；省略=全部",
    ("moodle_due", "days"): "未来天数，默认14",
    ("moodle_assignments", "course"): "课程号，如 FIT2102；省略=全部",
    ("moodle_assignments", "missing_only"): "true=仅可能漏交的作业",
    ("moodle_grades", "course"): "课程号；省略=成绩总览",
    ("moodle_grades", "graded_only"): "true=仅已有成绩的项目",
    ("moodle_forum", "query"): "搜索词；省略=最近公告",
    ("moodle_forum", "course"): "课程号，如 FIT2102；省略=全部",
    ("moodle_forum", "limit"): "数量参数；默认公告5、搜索10",
    ("moodle_messages", "conversation_id"): "对话 ID；省略=列对话",
    ("moodle_messages", "limit"): "数量上限；默认对话20、消息30",
    ("search_content", "query"): "全文搜索关键词或短语",
    ("search_content", "course"): "课程号，如 FIT2102；省略=全部",
    ("search_content", "limit"): "命中上限，默认15，最高50",
    ("list_files", "course"): "课程号，如 FIT2102；省略=全部",
    ("list_files", "week"): "教学周编号，如 8",
    ("list_files", "query"): "按文件名或路径筛选",
    ("list_files", "links"): "true=列外链；须同时给 course",
    ("read_file", "path"): "list_files/search_content 返回的路径",
    ("read_file", "inner"): "ZIP 内路径；省略=列 ZIP 清单",
    ("read_file", "offset"): "字符起点，默认0",
    ("read_file", "max_chars"): "最大字符数，默认20000，最高100000",
    ("ed_updates", "course"): "课程号，如 FIT2102；省略=全部",
    ("ed_updates", "since"): "起始时间：7d/12h/2w 或 ISO 日期时间",
    ("ed_updates", "peek"): "true=查看但不推进更新游标",
    ("ed_updates", "limit"): "活动上限，默认30",
    ("ed_threads", "query"): "搜索标题/正文；省略=浏览帖子",
    ("ed_threads", "course"): "课程号，如 FIT2102；省略=全部",
    ("ed_threads", "since"): "起始时间：7d/12h/2w 或 ISO 日期时间",
    ("ed_threads", "type"): "帖子类型",
    ("ed_threads", "unanswered"): "true=仅未回答问题",
    ("ed_threads", "only"): "状态筛选：收藏/关注/未看/本人/未读回复",
    ("ed_threads", "category"): "Ed 分类",
    ("ed_threads", "limit"): "条数上限，默认15，最高50",
    ("ed_threads", "offset"): "浏览结果偏移；搜索时忽略",
    ("ed_thread", "thread"): "帖子 ID、FIT2102#42 或 Ed 链接",
    ("ed_thread", "live"): "true=先从 Ed 实时刷新该帖",
    ("ed_thread", "offset"): "Markdown 字符起点，默认0",
    ("ed_thread", "max_chars"): "最大字符数，默认30000，最高100000",
    ("ed_lessons", "course"): "课程号，如 FIT2102",
    ("ed_lessons", "module"): "可选模块名称或编号",
    ("ed_lessons", "lesson"): "Lesson 名称或编号；有值=读正文",
    ("ed_lessons", "status"): "完成状态筛选",
    ("ed_lessons", "quiz"): "true=返回测验而非 Lesson 列表",
    ("ed_lessons", "offset"): "正文字符起点，默认0",
    ("ed_lessons", "max_chars"): "最大字符数，默认30000，最高100000",
}

def tool_list() -> list[dict]:
    out = []
    for name, spec in TOOLS.items():
        _, desc, params = spec[:3]
        read_only = spec[3] if len(spec) > 3 else True
        props = {}
        for k, p in params.items():
            prop = {"type": p[0], **({"enum": list(p[2])} if len(p) > 2 else {})}
            if (help_text := PARAM_HELP.get((name, k))):
                prop["description"] = help_text
            props[k] = prop
        required = [k for k, p in params.items() if p[1]]
        out.append({"name": name, "description": desc,
                    "inputSchema": {"type": "object", "properties": props, "required": required},
                    "annotations": {"readOnlyHint": read_only}})
    return out


def call_tool(name: str, arguments: dict) -> dict:
    try:
        data = TOOLS[name][0](arguments or {})
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return {"content": [{"type": "text", "text": _clip(text, MAX_OUT)}]}
    except MoodleAuthError as e:
        hint = "截止日期还能先用 moodle_due 查（走日历订阅链接，不需要登录）。" \
            if name != "moodle_due" and F.load_calendar_url() else ""
        return {"isError": True, "content": [{"type": "text", "text":
                f"Moodle 登录已过期或还没登录（{e}）。问用户要不要现在登录；同意的话调用 monash_login，"
                f"会在用户电脑上打开登录窗口。{hint}"}]}
    except EdAuthError as e:
        return {"isError": True, "content": [{"type": "text", "text":
                f"Ed 令牌没设置或失效了（{e}）。请用户在终端运行 `monash login ed`，"
                "按提示在 Ed 设置页新建令牌后粘贴进去（不要把令牌发到聊天里）。"}]}
    except (LookupError, ValueError) as e:
        return {"isError": True, "content": [{"type": "text", "text": str(e)}]}


# ---------------------------------------------------------------- 协议

def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # 通知（notifications/initialized 等）不用回
    try:
        if method == "initialize":
            result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                      "capabilities": {"tools": {}, "prompts": {}},
                      "serverInfo": {"name": "monash", "version": __version__},
                      "instructions": INSTRUCTIONS + _update_hint()}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_list()}
        elif method == "prompts/list":
            result = {"prompts": help_menu.prompt_list()}
        elif method == "prompts/get":
            p = msg.get("params") or {}
            try:
                result = help_menu.get_prompt(p.get("name"), p.get("arguments"))
            except KeyError:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"没有提示 {p.get('name')}"}}
            except ValueError as e:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}}
        elif method == "tools/call":
            p = msg.get("params") or {}
            name = p.get("name")
            if name not in TOOLS:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"没有工具 {name}"}}
            result = call_tool(name, p.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持 {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # noqa: BLE001
        log("工具出错：\n" + traceback.format_exc())
        return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True,
                "content": [{"type": "text", "text": f"出错了：{e}"}]}}


def main(background: bool = True) -> None:
    # Windows 上管道默认是本地代码页（cp1252/cp936），中文会乱码甚至报错；协议要求 UTF-8
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    ensure_private_dir(HOME)
    log(f"MCP 启动（{__version__}）")
    if background:
        threading.Thread(target=_background, name="background", daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
