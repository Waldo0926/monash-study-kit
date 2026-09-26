"""本周待办：把 Moodle 和 Ed 两边“要我做点什么”的东西合成一张清单。

    monash todo [--days 7]

  - Moodle：接下来 N 天的截止/日历事件（会话失效时用 iCal 订阅链接）、已过截止但没交也没分的作业；
  - Ed：还没做完的 lesson（只到你目前进度的下一周）、最近 N 天的公告、有未读新回复的帖子。

Ed 的部分读本地的 ed.db（只读打开），不发请求；库由 sync 维护。
Moodle 某一块拿不到（比如会话失效后作业汇总要登录）就在 notes 里说一声，其余照常给。
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import features as F
from .edlib import DB_PATH as ED_DB
from .moodlelib import MoodleAuthError, MoodleClient
from .syncer import course_code

WEEK_RE = re.compile(r"\bW(?:eek)?\s*(\d{1,2})\b", re.I)


def _week(*texts: str | None) -> int | None:
    for t in texts:
        m = WEEK_RE.search(t or "")
        if m:
            return int(m.group(1))
    return None


def _code(s: str | None) -> str:
    m = re.search(r"[A-Z]{3}\d{4}", s or "")
    return m.group(0) if m else (s or "").strip()


def _ed_conn() -> sqlite3.Connection | None:
    db = ED_DB
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def ed_lessons_todo(con: sqlite3.Connection) -> list[dict]:
    """没做完的 lesson，只到“目前进度的下一周”。

    Ed 的 lesson 没有截止日期，也不知道现在是第几周（Monash 有期中假，按开学日推算会差一周），
    所以用你自己的进度判断：每门课里做过（attempted/completed）的最大周次 + 1。
    这样落下的前几周会列出来，还没到的后几周不会刷屏。
    """
    rows = con.execute("""SELECT l.id, l.course_id, l.title, l.status, m.name AS module, c.code
                          FROM lessons l LEFT JOIN modules m ON m.id = l.module_id
                          JOIN courses c ON c.id = l.course_id
                          WHERE c.tracked = 1 AND COALESCE(l.is_hidden, 0) = 0""").fetchall()
    by_course: dict[int, list] = {}
    for r in rows:
        by_course.setdefault(r["course_id"], []).append(r)
    out = []
    for cid, ls in by_course.items():
        done_weeks = [w for r in ls if r["status"] in ("attempted", "completed")
                      and (w := _week(r["title"], r["module"])) is not None]
        upto = (max(done_weeks) + 1) if done_weeks else 1
        for r in ls:
            w = _week(r["title"], r["module"])
            if r["status"] == "completed" or w is None or w > upto:
                continue
            out.append({"course": _code(r["code"]), "week": w, "title": r["title"], "status": r["status"],
                        "url": f"https://edstem.org/au/courses/{cid}/lessons/{r['id']}"})
    return sorted(out, key=lambda x: (x["course"], x["week"], x["title"]))


def ed_announcements(con: sqlite3.Connection, days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = con.execute("""SELECT t.number, t.title, t.created_at, t.url, c.code FROM threads t
                          JOIN courses c ON c.id = t.course_id
                          WHERE c.tracked = 1 AND t.type = 'announcement'
                            AND julianday(t.created_at) >= julianday(?)
                          ORDER BY julianday(t.created_at) DESC""", (since,)).fetchall()
    return [{"course": _code(r["code"]), "source": "ed", "ref": f"{_code(r['code'])}#{r['number']}",
             "title": r["title"], "time": r["created_at"], "url": r["url"]} for r in rows]


def ed_unread_replies(con: sqlite3.Connection) -> list[dict]:
    try:
        rows = con.execute("""SELECT t.number, t.title, t.url, c.code, u.new_reply_count, u.is_mine, u.is_watched
                              FROM thread_state u JOIN threads t ON t.id = u.thread_id
                              JOIN courses c ON c.id = t.course_id
                              WHERE c.tracked = 1 AND u.new_reply_count > 0
                              ORDER BY (u.is_mine OR u.is_watched) DESC, julianday(t.updated_at) DESC""").fetchall()
    except sqlite3.OperationalError:       # 还没同步过 Ed
        return []
    return [{"ref": f"{_code(r['code'])}#{r['number']}", "title": r["title"], "new": r["new_reply_count"],
             "mine_or_watched": bool(r["is_mine"] or r["is_watched"]), "url": r["url"]} for r in rows]


def _ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def build(client: MoodleClient | None, courses: list[dict], days: int = 7) -> dict:
    notes: list[str] = []
    out: dict = {"days": days, "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes")}

    try:
        out["due"] = [d for d in F.due(client, days, include_overdue_days=0) if not d["overdue"]]
        if out["due"] and out["due"][0]["source"] == "ical":
            notes.append("Moodle 会话失效：截止日期来自日历订阅链接，没有提交状态")
    except MoodleAuthError:
        out["due"] = []
        notes.append("Moodle 没登录（或登录已过期），也没有日历订阅链接：截止日期缺失")

    missing, news = [], []
    now = int(time.time())
    for c in courses:
        try:
            missing += [a for a in F.assignments(client, c) if a["missing"]]
            news += [{"course": course_code(c), "source": "moodle", "ref": None, "title": n["title"],
                      "time": n["time"], "url": n.get("url")}
                     for n in F.news(client, c, 3) if (n.get("time_ts") or 0) >= now - days * 86400]
        except MoodleAuthError:
            notes.append("Moodle 会话失效：作业汇总和 Moodle 公告缺失")
            break
    out["possibly_missing"] = missing

    con = _ed_conn()
    if con is None:
        notes.append("还没同步过 Ed（没设置令牌，或者第一次同步还没跑完）：Ed 部分缺失")
        out.update(ed_lessons=[], ed_unread_replies=[])
    else:
        with con:
            try:
                out["ed_lessons"] = ed_lessons_todo(con)
            except sqlite3.OperationalError:
                out["ed_lessons"] = []
            news += ed_announcements(con, days)
            out["ed_unread_replies"] = ed_unread_replies(con)
        con.close()
    out["announcements"] = sorted(news, key=lambda n: _ts(n["time"]), reverse=True)
    out["notes"] = notes
    return out
