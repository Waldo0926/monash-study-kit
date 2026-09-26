"""同步、续期、状态：`monash sync` 和 MCP 的后台线程共用。

一次完整同步 = Ed 帖子 + Ed Lessons（有令牌的话）→ Moodle 课件（登录着的话）→ 全文索引。
哪一边没登录就跳过那一边，在状态里记一笔，不影响另一边。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import content_index, edlib, edsync, lessons
from .edlib import EdAuthError
from .moodlelib import MoodleAuthError, MoodleClient, db_connect, now_iso, set_state
from .paths import HOME
from .syncer import CourseSync, clean, course_code, course_folder, write_links_md

STATUS_FILE = HOME / "status.json"
LOCK_FILE = HOME / "sync.lock"


# ---------------------------------------------------------------- Moodle 课程

def load_courses(client: MoodleClient, con) -> list[dict]:
    courses = client.courses()
    for c in courses:
        con.execute("INSERT INTO courses (id, code, folder, fullname) VALUES (?,?,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET code=excluded.code, folder=excluded.folder,"
                    " fullname=excluded.fullname",
                    (c["id"], course_code(c), course_folder(c), clean(c["fullname"])))
    con.commit()
    return courses


TERM_RE = re.compile(r"\b(S1|S2|Summer|Winter)\b(?:\s+MUM)?\s*(20\d\d)?", re.I)
TERM_ORDER = {"s1": 1, "winter": 2, "s2": 3, "summer": 4}


def course_term(course: dict) -> tuple[int, int] | None:
    """课名或分类里的学期标签 → (年, 学期序号)。"FIT2102 … - S2 2026" → (2026, 3)。"""
    text = f"{clean(course.get('fullname', ''))} {course.get('coursecategory') or ''}"
    best = None
    for m in TERM_RE.finditer(text):
        year = m.group(2) or (re.search(r"20\d\d", text) or [None])[0]
        if year:
            t = (int(year), TERM_ORDER[m.group(1).lower()])
            best = max(best, t) if best else t
    return best


def current_courses(courses: list[dict], now: float | None = None) -> list[dict]:
    """本学期的课。

    Moodle 上的开课/结课日期不可靠（有的课 3 月就“开课”、下学期的课也已经挂出来），所以主要看
    课名/分类里的学期标签：已经开课的课里，最新的那个学期就是本学期。没有学期标签的课退回按日期判断。
    """
    now = now or time.time()
    coded = [c for c in courses if course_code(c)]
    started = [c for c in coded if (c.get("startdate") or 0) <= now + 7 * 86400]
    terms = [t for c in started if (t := course_term(c))]
    if terms:
        cur = max(terms)
        return [c for c in started if course_term(c) == cur]
    return [c for c in started if (c.get("enddate") or now + 1) > now
            and (c.get("startdate") or 0) > now - 200 * 86400]


def tracked_courses(con, courses: list[dict]) -> list[dict]:
    """跟踪的课；一门都没选过就自动选本学期的课（以后 `monash courses` 可以改）。"""
    tracked = {r["id"] for r in con.execute("SELECT id FROM courses WHERE tracked=1")}
    if not tracked:
        picked = current_courses(courses)
        con.executemany("UPDATE courses SET tracked=1 WHERE id=?", [(c["id"],) for c in picked])
        con.commit()
        return picked
    return [c for c in courses if c["id"] in tracked]


# ---------------------------------------------------------------- 同步

def sync_moodle(log=print, only: list[dict] | None = None, dry_run: bool = False) -> dict:
    client, con = MoodleClient(), db_connect()
    courses = load_courses(client, con)
    targets = only or tracked_courses(con, courses)
    total = {"courses": [], "downloaded": 0, "bytes": 0, "errors": 0}
    for course in targets:
        log(f"Moodle {course_code(course) or course_folder(course)}")
        stats = CourseSync(client, con, course, dry_run=dry_run, log=log).run()
        if not dry_run:
            write_links_md(con, course["id"], course_folder(course))
        total["courses"].append(course_code(course))
        total["downloaded"] += stats.downloaded
        total["bytes"] += stats.bytes
        total["errors"] += len(stats.errors)
    if not dry_run:
        set_state(con, "last_sync", {"at": now_iso(), "new_files": total["downloaded"]})
    con.close()
    return total


def sync_ed(log=print) -> dict:
    client = edlib.EdClient()
    out = edsync.sync(log=log, client=client)
    conn = edlib.db_connect()
    for (cid,) in conn.execute("SELECT id FROM courses WHERE tracked=1").fetchall():
        lessons.sync_lessons(conn, client, cid, log=log)
    conn.close()
    return out


def have_ed_token() -> bool:
    return bool(os.environ.get("ED_TOKEN")) or edlib.TOKEN_FILE.exists()


def sync_all(log=print) -> dict:
    """能同步的都同步一遍。另一个进程正在同步就直接返回。"""
    if not _lock():
        log("另一个同步正在进行，这次跳过")
        return read_status()
    try:
        status = read_status()
        status["errors"] = {}
        if have_ed_token():
            try:
                status["ed"] = {**sync_ed(log), "at": now_iso()}
            except EdAuthError as e:
                status["errors"]["ed"] = str(e)
            except Exception as e:  # noqa: BLE001 —— 网络问题之类，下次再试
                status["errors"]["ed"] = f"{type(e).__name__}: {e}"
        else:
            status["errors"]["ed"] = "还没设置 Ed 令牌（monash login ed）"
        try:
            status["moodle"] = {**sync_moodle(log), "at": now_iso()}
        except MoodleAuthError as e:
            status["errors"]["moodle"] = str(e)
        except Exception as e:  # noqa: BLE001
            status["errors"]["moodle"] = f"{type(e).__name__}: {e}"
        try:
            status["index"] = content_index.index(db_connect())
        except Exception as e:  # noqa: BLE001
            status["errors"]["index"] = f"{type(e).__name__}: {e}"
        status["last_sync"] = now_iso()
        write_status(status)
        return status
    finally:
        _unlock()


def keepalive() -> int | None:
    """给 Moodle 会话续期一次（会话空闲 4 小时就过期）。返回剩余秒数。"""
    left = MoodleClient().touch()
    status = read_status()
    status["session"] = {"alive": True, "checked_at": now_iso(), "time_remaining": left}
    write_status(status)
    return left


# ---------------------------------------------------------------- 状态文件

def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(status: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATUS_FILE)


def hours_since_sync(status: dict | None = None) -> float:
    at = (status or read_status()).get("last_sync")
    if not at:
        return float("inf")
    return (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() / 3600


def _lock() -> bool:
    """跨进程的同步锁（CLI 和 MCP 可能同时想同步）。超过 2 小时的锁当成上次崩了留下的。"""
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - LOCK_FILE.stat().st_mtime > 7200:
            LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(LOCK_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _unlock() -> None:
    Path(LOCK_FILE).unlink(missing_ok=True)
