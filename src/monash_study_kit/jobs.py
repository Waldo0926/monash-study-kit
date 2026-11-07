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
from .moodlelib import DATA_DIR, MoodleAuthError, MoodleClient, db_connect, get_state, now_iso, set_state
from .paths import HOME, FILES_DIR, load_settings
from .local_state import atomic_write, file_lock
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

def sync_moodle(log=print, only: list[dict] | None = None, dry_run: bool = False, refresh_google: bool = False, retry_failures: list | None = None) -> dict:
    client, con = MoodleClient(), db_connect()
    try:
        courses = load_courses(client, con)
        targets = only or tracked_courses(con, courses)
        total = {"courses": [], "downloaded": 0, "bytes": 0, "removed": 0, "errors": 0, "failures": []}
        for course in targets:
            cmids = None if retry_failures is None else {f["cmid"] for f in retry_failures if f["course_id"] == course["id"]}
            if cmids == set():
                continue
            log(f"Moodle {course_code(course) or course_folder(course)}")
            stats = CourseSync(client, con, course, dry_run=dry_run, log=log, only_cmids=cmids).run()
            if not dry_run and retry_failures is None:
                from . import google_export
                econ = edlib.db_connect()
                try:
                    google_export.sync_course(con, course, ed_conn=econ, refresh=refresh_google, log=log)
                finally:
                    econ.close()
                write_links_md(con, course["id"], course_folder(course))
            if not dry_run:
                from . import changes
                changes.files(con, course["id"], course_code(course), FILES_DIR)
            total["courses"].append(course_code(course))
            total["downloaded"] += stats.downloaded
            total["bytes"] += stats.bytes
            total["removed"] += stats.removed
            total["errors"] += len(stats.errors)
            total["failures"] += [{"course_id": course["id"], "cmid": cid} for cid in stats.failed_cmids]
        if not dry_run:
            set_state(con, "last_sync", {"at": now_iso(), "new_files": total["downloaded"]})
        return total
    finally:
        con.close()


def sync_ed(log=print, course_code: str | None = None, lessons_only: bool = False, retry_failures: list | None = None) -> dict:
    """lessons_only：只同步 Lessons 课件，不抓论坛帖子（导出课程资料时用）。"""
    client = edlib.EdClient()
    ids = None
    materials = lessons_only or load_settings().get("materials_only", False)
    listing = client.courses() if materials or course_code else None
    if course_code:
        ids = [c["id"] for c in listing if course_code.upper() in re.split(r"[/\s]+", (c.get("code") or "").upper())]
        if not ids:
            return {"new_threads": 0, "new_replies": 0}
    if materials:
        conn = edlib.db_connect()
        try:
            edsync.upsert_courses(conn, listing)
            edsync.auto_track(conn, listing)
        finally:
            conn.close()
        out = {"new_threads": 0, "new_replies": 0}
    else:
        out = edsync.sync(course_ids=ids, log=log, client=client,
                          only_threads=None if retry_failures is None else {f["thread_id"] for f in retry_failures if "thread_id" in f})
    out.setdefault("failures", [])
    conn = edlib.db_connect()
    try:
        targets = ids if ids is not None else [r[0] for r in conn.execute("SELECT id FROM courses WHERE tracked=1")]
        for cid in targets:
            wanted = None if retry_failures is None else {f["lesson_id"] for f in retry_failures if f.get("course_id") == cid and "lesson_id" in f}
            if wanted == set():
                continue
            lessons.sync_lessons(conn, client, cid, log=log, failures=out["failures"], only_lessons=wanted)
    finally:
        conn.close()
    out["failed"] = len(out["failures"])
    return out


def have_ed_token() -> bool:
    return bool(os.environ.get("ED_TOKEN")) or edlib.TOKEN_FILE.exists()


def sync_all(log=print, refresh_google: bool = False, retry_failed: bool = False) -> dict:
    """能同步的都同步一遍。另一个进程正在同步就直接返回。"""
    if not _lock():
        log("另一个同步正在进行，这次跳过")
        return read_status()
    try:
        previous = read_status()
        status = {"errors": {}, "sync_started_at": now_iso()}
        def needed(name):
            old = previous.get(name) or {}
            return not retry_failed or name in previous.get("errors", {}) or bool(old.get("failed") or old.get("errors"))
        def retry_args(name):
            old = previous.get(name) or {}
            return {"retry_failures": old["failures"]} if retry_failed and old.get("failures") else {}

        if not needed("ed"):
            status["ed"] = previous.get("ed", {})
        elif have_ed_token():
            try:
                status["ed"] = {**sync_ed(log, **retry_args("ed")), "at": now_iso()}
            except EdAuthError as e:
                status["errors"]["ed"] = str(e)
            except Exception as e:  # noqa: BLE001  网络问题之类，下次再试
                status["errors"]["ed"] = f"{type(e).__name__}: {e}"
        elif load_settings().get("use_ed", True):
            status["errors"]["ed"] = "还没设置 Ed 令牌（monash login ed；不用 Ed 就 monash config use_ed false）"
        try:
            status["moodle"] = ({**sync_moodle(log, refresh_google=refresh_google, **retry_args("moodle")), "at": now_iso()}
                                if needed("moodle") else previous.get("moodle", {}))
        except MoodleAuthError as e:
            status["errors"]["moodle"] = str(e)
        except Exception as e:  # noqa: BLE001
            status["errors"]["moodle"] = f"{type(e).__name__}: {e}"
        if load_settings().get("web_notes", True) and needed("web_notes"):
            try:
                from . import webnotes
                con = db_connect()
                status["web_notes"] = webnotes.sync(con, log)
                con.close()
            except Exception as e:  # noqa: BLE001  讲义站抓不到不影响别的
                status["errors"]["web_notes"] = f"{type(e).__name__}: {e}"
        try:
            con = db_connect()
            try:
                material_retry = needed("moodle") or (needed("ed") and (
                    "ed" in previous.get("errors", {}) or any(
                        "lesson_id" in f for f in previous.get("ed", {}).get("failures", []))))
                if not retry_failed or material_retry:
                    status["index"] = content_index.index(con)
                elif previous.get("index", {}).get("failures"):
                    status["index"] = content_index.index(con, only_paths={
                        f["path"] for f in previous["index"]["failures"]})
                else:
                    status["index"] = previous.get("index", {})
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            status["errors"]["index"] = f"{type(e).__name__}: {e}"
        status["last_sync"] = now_iso()  # 兼容旧客户端：这是尝试时间
        partial = bool(status["errors"]) or any(
            isinstance(v, dict) and (v.get("failed") or v.get("errors"))
            for k, v in status.items() if k != "errors")
        status["sync_state"] = "partial" if partial else "complete"
        if not partial:
            status["last_complete_sync"] = status["last_sync"]
        return update_status(status)
    finally:
        _unlock()


def keepalive() -> int | None:
    """给 Moodle 会话续期一次（会话空闲 4 小时就过期）。返回剩余秒数。"""
    left = MoodleClient().touch()
    update_status({"session": {"alive": True, "state": "valid", "checked_at": now_iso(), "time_remaining": left}})
    return left


# ---------------------------------------------------------------- 状态文件

def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_status(status: dict) -> None:
    with file_lock(STATUS_FILE.with_suffix(".lock")):
        atomic_write(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=2))


def update_status(values: dict) -> dict:
    """锁住整个读取、合并、写入过程，续期和同步不互相覆盖。"""
    with file_lock(STATUS_FILE.with_suffix(".lock")):
        status = read_status()
        status.update(values)
        atomic_write(STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=2))
        return status


def last_sync_at(status: dict | None = None) -> str | None:
    """上次同步的时间。服务器上自己部署时课件是另一个脚本同步的，不写 status.json，
    只在库里记 last_sync，所以 status.json 没有就看库里的。"""
    at = (status or read_status()).get("last_sync")
    if at:
        return at
    if not (DATA_DIR / "moodle.db").exists():
        return None
    try:
        con = db_connect()
        try:
            return (get_state(con, "last_sync") or {}).get("at")
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None


def hours_since_sync(status: dict | None = None) -> float:
    at = last_sync_at(status)
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
