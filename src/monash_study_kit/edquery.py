"""查本地库：列帖子、搜帖子、读帖子、看新动态、课程内容。

CLI（monash ed …）和 MCP 服务器共用这里。只读 SQLite，不碰 Ed 的接口——库由 sync 维护
（MCP 在跑的时候后台每小时一次）。要最新状态就先 sync 或 show --live。

每个函数都返回普通的 dict/list，方便直接转 JSON。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import edlib


class NotFound(LookupError):
    """课程或帖子不存在（CLI 退出码 4）。"""


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------

def parse_since(value: str | None) -> str | None:
    """'7d' / '12h' / '2w' / '30m' 或 ISO 日期时间 -> UTC ISO 字符串。"""
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", value)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
                 "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"看不懂的时间 “{value}”：用 7d / 12h / 2w 或 2026-09-01 这种格式") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()   # 没写时区就按本机时区
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _short_code(code: str | None) -> str:
    """'FIT2109 S2 2026 Malaysia ' -> 'FIT2109'，'FIT3162/FIT3164 MUM…' -> 'FIT3162/FIT3164'。"""
    return (code or "").split()[0] if code else ""


def courses(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT c.id, c.code, c.name, c.tracked,
               (SELECT COUNT(*) FROM threads t WHERE t.course_id = c.id) AS threads,
               (SELECT MAX(created_at) FROM threads t WHERE t.course_id = c.id) AS latest
        FROM courses c ORDER BY c.tracked DESC, c.id""").fetchall()
    return [{"id": r["id"], "code": _short_code(r["code"]), "name": r["name"],
             "tracked": bool(r["tracked"]), "threads": r["threads"], "latest": r["latest"]} for r in rows]


def resolve_course(conn: sqlite3.Connection, ref) -> dict:
    """课程 id、课号（FIT2102）或课号/课名的一部分（3164、paradigms）。"""
    ref_s = str(ref).strip()
    rows = [dict(r) for r in conn.execute("SELECT id, code, name, tracked FROM courses")]
    if ref_s.isdigit():
        for r in rows:
            if str(r["id"]) == ref_s:
                return r
    low = ref_s.lower()
    exact = [r for r in rows if low in [p.lower() for p in re.split(r"[\s/]+", r["code"] or "") if p]]
    fuzzy = [r for r in rows if low in (r["code"] or "").lower() or low in (r["name"] or "").lower()]
    for group in (exact, fuzzy):
        if group:
            group.sort(key=lambda r: (not r["tracked"], -r["id"]))
            return group[0]
    known = "、".join(_short_code(r["code"]) or str(r["id"]) for r in rows) or "（库里还没有课程）"
    raise NotFound(f"没有匹配 “{ref}” 的课程。已有：{known}")


def resolve_thread(conn: sqlite3.Connection, ref) -> int:
    """帖子 id、'FIT2102#42'（课号+帖子编号）、'#42'（只有一门课时）或 Ed 帖子链接。"""
    ref_s = str(ref).strip()
    m = re.search(r"/discussion/(\d+)", ref_s)
    if m:
        ref_s = m.group(1)
    if ref_s.isdigit():
        row = conn.execute("SELECT id FROM threads WHERE id=?", (int(ref_s),)).fetchone()
        if row:
            return row["id"]
        raise NotFound(f"库里没有帖子 {ref_s}（可能还没 sync 到，或者不在跟踪的课里）")
    m = re.fullmatch(r"(.*?)#(\d+)", ref_s)
    if not m:
        raise NotFound(f"看不懂的帖子 “{ref}”：用帖子 id、FIT2102#42 或 Ed 链接")
    course_ref, number = m.group(1).strip(), int(m.group(2))
    if course_ref:
        cids = [resolve_course(conn, course_ref)["id"]]
    else:
        cids = [r["id"] for r in conn.execute("SELECT id FROM courses WHERE tracked=1")]
    rows = conn.execute(f"SELECT id FROM threads WHERE number=? AND course_id IN ({','.join('?' * len(cids))})",
                        (number, *cids)).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if not rows:
        raise NotFound(f"没有帖子 {ref_s}")
    raise NotFound(f"“#{number}” 在好几门课里都有，写成 课号#{number}")


# --------------------------------------------------------------------------
# 列表 / 搜索
# --------------------------------------------------------------------------

_LIST_COLS = """
    t.id, t.course_id, c.code AS course_code, t.number, t.type, t.title, t.category, t.subcategory,
    t.author, t.is_pinned, t.is_private, t.is_answered, t.is_staff_answered, t.reply_count,
    t.vote_count, t.created_at, t.updated_at, t.url,
    u.is_starred, u.is_watched, u.is_seen, u.new_reply_count, u.accepted_id, u.is_mine
"""
_LIST_FROM = """
    FROM threads t JOIN courses c ON c.id = t.course_id
    LEFT JOIN thread_state u ON u.thread_id = t.id
"""
# threads 命令的筛选开关 → SQL（都依赖 thread_state，同步过一次才有）
STATE_FILTERS = {
    "starred": "u.is_starred = 1",
    "watching": "u.is_watched = 1",
    "unseen": "u.is_seen = 0",
    "mine": "u.is_mine = 1",
    "unread_replies": "u.new_reply_count > 0",
}


def _thread_row(r) -> dict:
    d = dict(r)
    d["course"] = _short_code(d.pop("course_code"))
    d["ref"] = f"{d['course']}#{d['number']}"
    for k in ("is_pinned", "is_private", "is_answered", "is_staff_answered",
              "is_starred", "is_watched", "is_seen", "is_mine"):
        d[k] = bool(d[k])
    d["new_reply_count"] = d["new_reply_count"] or 0
    return d


def _filters(conn, course=None, since=None, category=None, type_=None, unanswered=False, state=()):
    where, params = [STATE_FILTERS[k] for k in state], []
    if course:
        where.append("t.course_id = ?")
        params.append(resolve_course(conn, course)["id"])
    else:
        where.append("c.tracked = 1")
    if since:
        where.append("julianday(t.created_at) >= julianday(?)")   # Ed 的时间带 +10:00，别按字符串比
        params.append(parse_since(since))
    if category:
        where.append("(LOWER(t.category) LIKE ? OR LOWER(t.subcategory) LIKE ?)")
        params += [f"%{category.lower()}%"] * 2
    if type_:
        where.append("t.type = ?")
        params.append(type_)
    if unanswered:
        where.append("t.type = 'question' AND t.is_answered = 0 AND t.is_staff_answered = 0")
    return where, params


def list_threads(conn, course=None, since=None, category=None, type_=None,
                 unanswered=False, limit=20, offset=0, state=()) -> list[dict]:
    """state：STATE_FILTERS 里的键，比如 ("starred",)、("mine", "unread_replies")，全部满足。"""
    where, params = _filters(conn, course, since, category, type_, unanswered, state)
    rows = conn.execute(
        f"SELECT {_LIST_COLS} {_LIST_FROM} WHERE {' AND '.join(where)} "
        "ORDER BY julianday(t.created_at) DESC LIMIT ? OFFSET ?", (*params, int(limit), int(offset))).fetchall()
    return [_thread_row(r) for r in rows]


def _snippet(text: str, words: list[str], width: int = 160) -> str:
    low = text.lower()
    hits = [low.find(w) for w in words if low.find(w) >= 0]
    start = max(0, min(hits) - width // 3) if hits else 0
    part = re.sub(r"\s+", " ", text[start:start + width]).strip()
    return ("…" if start else "") + part + ("…" if start + width < len(text) else "")


def search(conn, query: str, course=None, since=None, category=None, type_=None,
           limit=20) -> list[dict]:
    """每个词都要出现（不分大小写），范围是标题、正文和全部回复。按最新排序。"""
    words = [w.lower() for w in query.split() if w.strip()]
    if not words:
        raise ValueError("搜索词是空的")
    where, params = _filters(conn, course, since, category, type_)
    haystack = ("LOWER(COALESCE(t.title,'') || ' ' || COALESCE(t.body,'') || ' ' || "
                "COALESCE((SELECT GROUP_CONCAT(r.text, ' ') FROM replies r WHERE r.thread_id = t.id), ''))")
    for w in words:
        where.append(f"INSTR({haystack}, ?) > 0")
        params.append(w)
    rows = conn.execute(
        f"SELECT {_LIST_COLS}, t.body {_LIST_FROM} WHERE {' AND '.join(where)} "
        "ORDER BY julianday(t.created_at) DESC LIMIT ?", (*params, int(limit))).fetchall()
    out = []
    for r in rows:
        d = _thread_row(r)
        body = d.pop("body") or ""
        if not any(w in (d["title"] or "").lower() + body.lower() for w in words):
            reps = conn.execute("SELECT text FROM replies WHERE thread_id=? ORDER BY created_at", (d["id"],))
            body = next((x["text"] for x in reps if any(w in (x["text"] or "").lower() for w in words)), body)
            d["match_in"] = "reply"
        d["snippet"] = _snippet(body, words)
        out.append(d)
    return out


# --------------------------------------------------------------------------
# 单帖
# --------------------------------------------------------------------------

def get_thread(conn, ref) -> dict:
    tid = resolve_thread(conn, ref)
    r = conn.execute(f"SELECT {_LIST_COLS}, t.body, t.last_fetched {_LIST_FROM} WHERE t.id=?", (tid,)).fetchone()
    thread = _thread_row(r)
    atts = [dict(a) for a in conn.execute(
        "SELECT reply_id, kind, name, url FROM attachments WHERE thread_id=? ORDER BY reply_id, rowid", (tid,))]
    thread["attachments"] = [{k: a[k] for k in ("kind", "name", "url")} for a in atts if a["reply_id"] == 0]
    thread["replies"] = []
    for x in conn.execute("SELECT * FROM replies WHERE thread_id=? ORDER BY created_at, id", (tid,)):
        thread["replies"].append({
            "id": x["id"], "parent_id": x["parent_id"], "kind": x["kind"], "depth": x["depth"],
            "author": x["author"], "is_staff": bool(x["is_staff"]), "is_endorsed": bool(x["is_endorsed"]),
            "vote_count": x["vote_count"], "created_at": x["created_at"], "text": x["text"],
            "attachments": [{k: a[k] for k in ("kind", "name", "url")} for a in atts if a["reply_id"] == x["id"]],
        })
    thread["replies"] = _nest_order(thread["replies"])
    return thread


def _nest_order(replies: list[dict]) -> list[dict]:
    """按对话顺序排：每条回复后面紧跟它的子回复（深度优先），同级按时间。"""
    children: dict = {}
    for r in replies:
        children.setdefault(r["parent_id"], []).append(r)
    ids = {r["id"] for r in replies}
    out: list[dict] = []

    def walk(pid):
        for r in children.get(pid, []):
            out.append(r)
            walk(r["id"])

    walk(None)
    for r in replies:                       # 父回复被删了的孤儿，放到最后
        if r["parent_id"] is not None and r["parent_id"] not in ids and r not in out:
            out.append(r)
    return out


def following(conn, course=None, limit=30) -> list[dict]:
    """Ed 上有我没看过的新回复的帖子（Ed 自己记的已读位置，跟浏览器里看到的数字一致）。

    我发的、关注的、收藏的排前面；每帖带上最后那几条新回复。
    new_reply_count 是 Ed 对“我看过的帖子”算的，没点开过的帖子不在这里（用 unseen）。
    """
    rows = list_threads(conn, course, limit=500, state=("unread_replies",))
    rows.sort(key=lambda t: (not (t["is_mine"] or t["is_watched"] or t["is_starred"]),
                             -_jd(t["updated_at"])))
    for t in rows[:limit]:
        reps = conn.execute("SELECT author, is_staff, kind, created_at, text FROM replies WHERE thread_id=? "
                            "ORDER BY julianday(created_at) DESC LIMIT ?", (t["id"], t["new_reply_count"])).fetchall()
        t["new_replies"] = [{"author": x["author"], "is_staff": bool(x["is_staff"]), "kind": x["kind"],
                             "created_at": x["created_at"], "text": x["text"]} for x in reversed(reps)]
    return rows[:limit]


def _jd(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def thread_markdown(t: dict) -> str:
    flags = [f for f, on in (("置顶", t["is_pinned"]), ("私密", t["is_private"]),
                             ("老师已答", t["is_staff_answered"]), ("★ 已收藏", t.get("is_starred")),
                             ("关注中", t.get("is_watched")), ("我发的", t.get("is_mine"))) if on]
    lines = [f"# {t['title']}",
             f"{t['ref']} · {t['type']} · {t['category'] or ''}"
             + (f" / {t['subcategory']}" if t.get("subcategory") else "")
             + f" · {t['author']} · {(t['created_at'] or '')[:16].replace('T', ' ')}"
             + (f" · {'，'.join(flags)}" if flags else ""),
             t["url"], ""]
    lines.append(t.get("body") or "（无正文）")
    if t["replies"]:
        lines += ["", f"## 回复（{len(t['replies'])}）"]
    for r in t["replies"]:
        pad = "  " * (r["depth"] or 0)
        tag = ("✔ 已采纳 " if t.get("accepted_id") and r["id"] == t["accepted_id"] else "") \
            + ("✅ " if r["is_endorsed"] else "")
        lines += ["", f"{pad}**{tag}{r['author']}** · {r['kind']} · {(r['created_at'] or '')[:16].replace('T', ' ')}"]
        lines += [pad + ln if ln else "" for ln in (r["text"] or "").split("\n")]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# 新动态（带游标）
# --------------------------------------------------------------------------

def _cursor_file():
    return edlib.DATA_DIR / "cursors.json"


def read_cursor(name: str) -> str | None:
    try:
        return json.loads(_cursor_file().read_text()).get(name)
    except (OSError, ValueError):
        return None


def write_cursor(name: str, value: str) -> None:
    path = _cursor_file()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data[name] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def new_activity(conn, since=None, course=None, cursor: str | None = "cli",
                 advance: bool = True, limit: int = 50) -> dict:
    """上次看过之后的新帖和老帖的新回复（按我们抓到的时间 first_seen 算）。

    since 不给就用游标（第一次默认 3 天）；advance=True 时把游标推到现在。
    """
    now = edlib.now_iso()
    start = parse_since(since) if since else (read_cursor(cursor) if cursor else None)
    start = start or parse_since("3d")
    course_id = resolve_course(conn, course)["id"] if course else None
    cond, params = ("t.course_id = ?", [course_id]) if course_id else ("c.tracked = 1", [])

    new_threads = [_thread_row(r) for r in conn.execute(
        f"SELECT {_LIST_COLS} {_LIST_FROM} WHERE {cond} AND t.first_seen > ? "
        "ORDER BY julianday(t.created_at) DESC LIMIT ?", (*params, start, int(limit)))]
    fresh_ids = {t["id"] for t in new_threads}

    replied = []
    for r in conn.execute(
            f"SELECT {_LIST_COLS} {_LIST_FROM} WHERE {cond} AND t.id IN "
            "(SELECT thread_id FROM replies WHERE first_seen > ?) ORDER BY julianday(t.updated_at) DESC LIMIT ?",
            (*params, start, int(limit))):
        if r["id"] in fresh_ids:
            continue
        d = _thread_row(r)
        d["new_replies"] = [
            {"author": x["author"], "is_staff": bool(x["is_staff"]), "kind": x["kind"],
             "created_at": x["created_at"], "text": x["text"]}
            for x in conn.execute("SELECT * FROM replies WHERE thread_id=? AND first_seen > ? "
                                  "ORDER BY created_at", (d["id"], start))]
        replied.append(d)

    last_run = conn.execute("SELECT finished_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if advance and cursor and not since:
        write_cursor(cursor, now)
    return {"since": start, "until": now, "last_sync": last_run["finished_at"] if last_run else None,
            "new_threads": new_threads, "threads_with_new_replies": replied}


# --------------------------------------------------------------------------
# 课程内容（Lessons）
# --------------------------------------------------------------------------

def lessons(conn, course, module=None, status=None) -> list[dict]:
    try:
        rows = conn.execute("""
            SELECT l.*, m.name AS module FROM lessons l LEFT JOIN modules m ON m.id = l.module_id
            WHERE l.course_id = ? ORDER BY l.module_id, l.idx, l.id""",
                            (resolve_course(conn, course)["id"],)).fetchall()
    except sqlite3.OperationalError:          # 还没跑过 lessons 同步，表不存在
        return []
    out = []
    for r in rows:
        if module and module.lower() not in (r["module"] or "").lower() and str(module) != str(r["module_id"]):
            continue
        if status and status != "all" and r["status"] != status:
            continue
        files = [dict(f) for f in conn.execute(
            "SELECT type, title, file_url, url, local_path FROM lesson_files WHERE lesson_id=? ORDER BY id",
            (r["id"],))]
        out.append({"id": r["id"], "module": r["module"], "title": r["title"], "type": r["type"],
                    "status": r["status"], "due_at": r["effective_due_at"] or r["due_at"],
                    "slides": r["slide_count"], "files": files,
                    "url": f"https://edstem.org/au/courses/{r['course_id']}/lessons/{r['id']}"})
    return out


def quizzes(conn, course, module=None, status=None) -> list[dict]:
    """lesson 里的测验题（题面 + 选项，没有答案——Ed 不对学生公开）。按 lesson、quiz 页、题号排。"""
    out = []
    for l in lessons(conn, course, module, status):
        try:
            rows = conn.execute("SELECT * FROM quiz_questions WHERE lesson_id=? ORDER BY slide_id, idx",
                                (l["id"],)).fetchall()
        except sqlite3.OperationalError:
            return []
        if not rows:
            continue
        slides: dict = {}
        for r in rows:
            slides.setdefault(r["slide_id"], {"title": r["slide_title"], "questions": []})["questions"].append(
                {"n": (r["idx"] or 0) + 1, "type": r["type"], "multiple": bool(r["multiple"]),
                 "question": r["question"], "options": json.loads(r["options"] or "[]")})
        out.append({"lesson": l["title"], "module": l["module"], "status": l["status"], "url": l["url"],
                    "quizzes": list(slides.values())})
    return out


def quiz_markdown(rows: list[dict]) -> str:
    if not rows:
        return "（没有测验题；先运行 monash sync 同步一次）\n"
    mark = {"completed": "✅", "attempted": "◐", "unattempted": "○"}
    out = ["> 题面和选项来自 Ed Lessons。Ed 不对学生公开答案，这里只有题目。", ""]
    for l in rows:
        out += [f"# {mark.get(l['status'], '·')} {l['lesson']}", l["url"], ""]
        for qz in l["quizzes"]:
            out += [f"## {qz['title'] or '测验'}", ""]
            for q in qz["questions"]:
                multi = "（多选）" if q["multiple"] else ""
                out.append(f"**{q['n']}.** {multi}{q['question']}")
                out += [f"   {chr(65 + i)}. {o}" for i, o in enumerate(q["options"])]
                out.append("")
    return "\n".join(out).rstrip() + "\n"
