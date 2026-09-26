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

from . import __version__, edlib, edquery, jobs, textextract
from . import features as F
from .edlib import EdAuthError
from .moodlelib import COOKIE_FILE, FILES_DIR, MoodleAuthError, MoodleClient, db_connect
from .paths import HOME, LOG_FILE, ensure_private_dir, load_settings
from .syncer import course_code, course_folder

PROTOCOL = "2025-06-18"
MAX_TEXT = 20000
MAX_OUT = 60000
ED_CURSOR = "mcp"      # 和命令行的游标（cli）分开记
TEXT_EXT = {".txt", ".md", ".py", ".hs", ".ts", ".js", ".java", ".c", ".cpp", ".h", ".sh", ".sql", ".csv",
            ".json", ".mzn", ".dzn", ".r", ".rmd", ".html", ".css", ".yaml", ".yml", ".toml", ".ipynb"}

INSTRUCTIONS = (
    "Monash 学习助手（Moodle + Ed，只读）。问“这周要做什么/有什么要交”先用 study_todo；"
    "Ed 上的新消息用 ed_new，找帖子用 ed_search，读全文用 ed_thread（写成 FIT2102#42）；"
    "找某个知识点在哪份课件/哪节录播里用 moodle_search_content，再用 moodle_read_file 读原文；"
    "录像的内容看同名 .transcript.md 字幕稿。数据每小时在后台同步一次。"
    "工具报“Moodle 登录已过期”时，问用户要不要现在登录，同意就调用 monash_login（会在用户电脑上打开登录窗口）。"
    "Ed 令牌没设置或失效时，请用户在终端运行 monash login ed（令牌不要贴进聊天）。"
    "不能交作业、做测验或发帖。")


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


def _background():
    """Claude 开着的时候：按设置定时同步、给 Moodle 续期。"""
    settings = load_settings()
    every_sync = max(0.25, float(settings.get("auto_sync_hours") or 1)) * 3600
    every_touch = max(5, int(settings.get("keepalive_minutes") or 20)) * 60
    time.sleep(5)       # 先让 Claude 把握手做完
    last_touch = 0.0
    while True:
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
    return {"moodle": moodle, "ed": ed, "last_sync": st.get("last_sync"), "sync_errors": st.get("errors"),
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
    return todo.build(c, courses, int(args.get("days", 7)))


def t_overview(args):
    c = client()
    return {"session_time_remaining_s": c.time_remaining(), "due_14_days": F.due(c, 14),
            "alerts": F.alerts(c, 5)}


def t_courses(args):
    return [{"id": c["id"], "code": course_code(c), "name": course_folder(c)} for c in _courses()]


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


def t_news(args):
    courses = [_course(args["course"])] if args.get("course") else _tracked_or_current()
    out = []
    for co in courses:
        out += F.news(client(), co, int(args.get("limit", 3)))
    out.sort(key=lambda x: -(x.get("time_ts") or 0))
    for n in out:
        n["text"] = n["text"][:1500]
        n.pop("time_ts", None)
    return out


def t_forum_search(args):
    cid = _course(args["course"])["id"] if args.get("course") else 1
    res = F.forum_search(client(), args["query"], cid, int(args.get("limit", 10)))
    for r in res:
        r["text"] = r["text"][:800]
    return res


def t_find(args):
    courses = [_course(args["course"])] if args.get("course") else _tracked_or_current()
    return [{k: v for k, v in h.items() if k != "score"}
            for h in F.find(client(), args["query"], courses, limit=int(args.get("limit", 15)))]


def _local_course_id(ref: str) -> int:
    """list_files / links 只读本地库，会话过期时也能用：先按本地课程表找，找不到再问 Moodle。"""
    con = db_connect()
    ref_u = ref.strip().upper()
    for r in con.execute("SELECT id, code, fullname FROM courses ORDER BY id DESC"):
        if str(r["id"]) == ref.strip() or (r["code"] or "") == ref_u or ref.lower() in (r["fullname"] or "").lower():
            return r["id"]
    return _course(ref)["id"]


def t_list_files(args):
    con = db_connect()
    q = "SELECT f.path, f.size, f.title FROM files f WHERE 1=1"
    params: list = []
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


def t_links(args):
    con = db_connect()
    q, params = "SELECT folder, section, title, url FROM links WHERE course_id=?", [_local_course_id(args["course"])]
    if args.get("week"):
        q += " AND folder LIKE ?"
        params.append(f"Week {int(args['week']):02d}%")
    return [dict(r) for r in con.execute(q + " ORDER BY folder, rowid LIMIT 200", params)]


def t_read_file(args):
    """读一个已同步的课件：文本直接给；PDF 按页抽文字；docx/pptx 抽文字；zip 先列清单。"""
    rel = args["path"].replace("\\", "/").lstrip("/")
    path = (FILES_DIR / rel).resolve()
    if FILES_DIR.resolve() not in path.parents or not path.is_file():
        raise ValueError("只能读 moodle_list_files 列出的文件（相对路径）")
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


def t_ed_courses(args):
    return edquery.courses(_ed())


def t_ed_new(args):
    d = edquery.new_activity(_ed(), args.get("since"), args.get("course"), cursor=ED_CURSOR,
                             advance=not args.get("peek", False), limit=int(args.get("limit", 40)))
    d["new_threads"] = _slim(d["new_threads"])
    d["threads_with_new_replies"] = [_with_replies(t) for t in d["threads_with_new_replies"]]
    return d


def t_ed_threads(args):
    rows = edquery.list_threads(_ed(), args.get("course"), args.get("since"), args.get("category"),
                                args.get("type"), bool(args.get("unanswered")),
                                min(int(args.get("limit", 20)), 100), int(args.get("offset", 0)),
                                [k for k in edquery.STATE_FILTERS if args.get(k)])
    return _slim(rows)


def t_ed_following(args):
    return [_with_replies(t) for t in edquery.following(_ed(), args.get("course"), min(int(args.get("limit", 20)), 50))]


def t_ed_search(args):
    return _slim(edquery.search(_ed(), args["query"], args.get("course"), args.get("since"),
                                args.get("category"), args.get("type"), min(int(args.get("limit", 15)), 50)))


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
    rows = edquery.lessons(_ed(), args["course"], args.get("module"), args.get("status"))
    for r in rows:
        for f in r["files"]:
            f.pop("local_path", None)
    return rows


def t_ed_quiz(args):
    rows = edquery.quizzes(_ed(), args["course"], args.get("module"), args.get("status"))
    return _clip(edquery.quiz_markdown(rows), min(int(args.get("max_chars", 30000)), 100000), int(args.get("offset", 0)))


# ---------------------------------------------------------------- 工具表

S, I, B = "string", "integer", "boolean"

TOOLS = {
    "study_todo": (t_todo, "本周待办（Moodle + Ed 合在一起）：N 天内的截止（默认 7）、可能漏交的作业、"
                           "Ed 上还没做完的 lesson（到目前进度的下一周为止）、最近的公告（两边）、Ed 上有未读新回复的帖子。"
                           "问“这周要做什么/有什么要交”先用这个。notes 里会说明哪部分因为没登录而缺失。",
                   {"days": (I, False)}),
    "monash_status": (t_status, "登录状态（Moodle 会话还剩多久、Ed 令牌有没有设置）、上次同步时间和错误、课件存放位置。"
                                "工具报错或用户问“连上了吗”时先看这个。用户说“用不了/出问题了”时传 full=true 做完整体检"
                                "（每项有 fix 字段说明怎么修，照着告诉用户）。", {"full": ("boolean", False)}),
    "monash_login": (t_login, "在用户电脑上打开 Moodle 专用登录窗口（先试后台自动登录）。只在用户同意后调用；"
                              "调用后立即返回，用户在窗口里登录完成后窗口自动关闭并开始同步。", {}, False),
    "monash_sync": (t_sync, "立刻在后台同步 Ed 和 Moodle（平时每小时自动一次）。用户说“刷新一下/同步一下”时用。", {}, False),
    "moodle_overview": (t_overview, "会话剩余时间、14 天内的截止项、未读通知。", {}),
    "moodle_courses": (t_courses, "列出已选的所有 Moodle 课程（id、课号、名称）。", {}),
    "moodle_due": (t_due, "截止日期和日历事件（作业截止、测验开放/关闭）。没登录时自动改用日历订阅链接，"
                          "这时 source=ical、没有提交状态。course 可选：课号如 FIT2102。",
                   {"course": (S, False), "days": (I, False)}),
    "moodle_assignments": (t_assignments, "作业汇总：每项的截止时间、提交状态、成绩。missing=true 表示已过截止、"
                                          "没交也没分（可能漏交；面试/现场展示这类本来不用在线交）。"
                                          "不给 course 就是跟踪的课；missing_only 只返回可能漏交的。",
                           {"course": (S, False), "missing_only": (B, False)}),
    "moodle_messages": (t_messages, "Moodle 站内私信。不给 conversation_id 列出对话（含未读数和最后一条）；"
                                    "给了就返回这个对话的消息。只读，不会标成已读。",
                        {"conversation_id": (I, False), "limit": (I, False)}),
    "moodle_search_content": (t_search_content, "全文搜课件内容：Moodle 的 PDF/文档、录播字幕稿（.transcript.md）、"
                                                "Ed Lessons 的课件 PDF。适合问“哪一周讲了 X”“X 在哪份讲义里”。"
                                                "每条带定位：loc 是页码（p.3）、字幕时间戳（12:34）或 slide 号；path 可以交给 "
                                                "moodle_read_file 读全文（Ed 课件没有 path）。多个词都要出现，\"引号\"是短语，词尾 * 是前缀。",
                              {"query": (S, True), "course": (S, False), "limit": (I, False)}),
    "moodle_grades": (t_grades, "成绩。不给 course 列各课总分；给了列每项的得分、满分、反馈。",
                      {"course": (S, False), "graded_only": (B, False)}),
    "moodle_news": (t_news, "Moodle 课程公告（含正文）。不给 course 就是跟踪的课。",
                    {"course": (S, False), "limit": (I, False)}),
    "moodle_forum_search": (t_forum_search, "搜 Moodle 论坛帖子（Monash 多数讨论在 Ed，这里主要是公告）。",
                            {"query": (S, True), "course": (S, False), "limit": (I, False)}),
    "moodle_find": (t_find, "按名字找 Moodle 的章节和活动，如 'week 7 slides'。返回活动 id 和链接。",
                    {"query": (S, True), "course": (S, False), "limit": (I, False)}),
    "moodle_list_files": (t_list_files, "已同步到本地的 Moodle 课件清单（按课程/周次过滤）。path 可给 moodle_read_file。"
                                        "有字幕稿的录像旁边是同名 .transcript.md；query='transcript' 只列字幕稿。",
                          {"course": (S, False), "week": (I, False), "query": (S, False)}),
    "moodle_links": (t_links, "某门课（某周）的外部链接：Slides、课程笔记、视频等。",
                     {"course": (S, True), "week": (I, False)}),
    "moodle_read_file": (t_read_file, "读一个已同步课件的文字（文本/代码/PDF/docx/pptx；zip 先列清单再用 inner 读其中一个）。"
                                      "默认最多 20000 字符，用 offset 分段。",
                         {"path": (S, True), "inner": (S, False), "offset": (I, False), "max_chars": (I, False)}),
    "ed_courses": (t_ed_courses, "本地库里的 Ed 课程：id、课号、是否跟踪、帖子数、最新发帖时间。", {}),
    "ed_new": (t_ed_new, "上次调用之后 Ed 上的新帖和老帖的新回复（含回复正文）。默认推进游标，下次只给更新的；"
                         "since（如 2d、2026-09-20）改为看这之后的全部且不动游标；peek=true 看完不推进。"
                         "结果里的 last_sync 是库最近一次同步时间。",
               {"course": (S, False), "since": (S, False), "peek": (B, False), "limit": (I, False)}),
    "ed_threads": (t_ed_threads, "列 Ed 帖子（最新在前）。course 是课号如 FIT2102，不给就是全部跟踪的课；since 如 7d；"
                                 "type 为 question/post/announcement；unanswered=true 只看没人答的提问；"
                                 "starred/watching/mine/unseen/unread_replies=true 只看我收藏的/关注的/我发的/"
                                 "还没点开过的/有我没看过的新回复的。",
                   {"course": (S, False), "since": (S, False), "category": (S, False), "type": (S, False),
                    "unanswered": (B, False), "starred": (B, False), "watching": (B, False), "mine": (B, False),
                    "unseen": (B, False), "unread_replies": (B, False), "limit": (I, False), "offset": (I, False)}),
    "ed_following": (t_ed_following, "我在 Ed 上看过、但有没看过的新回复的帖子（和 Ed 网页上的数字一致），"
                                     "我发的/关注的/收藏的排前面，附上那几条新回复。问“我的帖子有人回了吗”用这个。",
                     {"course": (S, False), "limit": (I, False)}),
    "ed_search": (t_ed_search, "搜 Ed 帖子标题、正文和全部回复，每个词都要出现（不分大小写）。返回命中片段和帖子引用（ref）。",
                  {"query": (S, True), "course": (S, False), "since": (S, False), "category": (S, False),
                   "type": (S, False), "limit": (I, False)}),
    "ed_thread": (t_ed_thread, "读一个 Ed 帖子的全文和全部回复（Markdown，链接、附件都在）。thread 可以是 FIT2102#42、"
                               "帖子 id 或 Ed 链接；live=true 先从 Ed 重抓（库最多晚一小时）。长帖用 offset 分段。",
                  {"thread": (S, True), "live": (B, False), "offset": (I, False), "max_chars": (I, False)}),
    "ed_lessons": (t_ed_lessons, "Ed 上的课程内容（Lessons）：按模块列出、完成状态、截止时间、课件 PDF 和网页链接。"
                                 "module 是模块名的一部分如 'Week 3'；status 为 completed/attempted/unattempted。",
                   {"course": (S, True), "module": (S, False), "status": (S, False)}),
    "ed_quiz": (t_ed_quiz, "Ed Lessons 里的测验题（题面 + 选项，Markdown）。Ed 不对学生公开答案，适合复习或出练习。"
                           "module 如 'Week 3'；status 为 completed/attempted/unattempted。长的用 offset 分段。",
                {"course": (S, True), "module": (S, False), "status": (S, False), "offset": (I, False),
                 "max_chars": (I, False)}),
}


def tool_list() -> list[dict]:
    out = []
    for name, spec in TOOLS.items():
        _, desc, params = spec[:3]
        read_only = spec[3] if len(spec) > 3 else True
        props = {k: {"type": t} for k, (t, _) in params.items()}
        required = [k for k, (_, req) in params.items() if req]
        out.append({"name": name, "description": desc,
                    "inputSchema": {"type": "object", "properties": props, "required": required},
                    "annotations": {"readOnlyHint": read_only, "openWorldHint": True}})
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
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "monash", "version": __version__},
                      "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": tool_list()}
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
