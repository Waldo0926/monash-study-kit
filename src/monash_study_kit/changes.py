"""本机课程变更日志：只保存指纹和定位元数据，不额外访问学校服务。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .paths import DATA_DIR, load_settings

DB = DATA_DIR / "changes.db"


def _connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS scopes (scope TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS snapshots (scope TEXT, key TEXT, value TEXT,
            PRIMARY KEY(scope,key));
        CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY, at TEXT, kind TEXT,
            course TEXT, title TEXT, url TEXT, before TEXT, after TEXT);
    """)
    return con


def observe(scope: str, items: list[dict]) -> None:
    """第一次建基线；不从列表缺项推断删除，避免部分同步造成假下架。"""
    con = _connect()
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            baseline = con.execute("SELECT 1 FROM scopes WHERE scope=?", (scope,)).fetchone() is None
            at = datetime.now(timezone.utc).isoformat()
            for item in items:
                value = json.dumps(item, sort_keys=True, ensure_ascii=False)
                row = con.execute("SELECT value FROM snapshots WHERE scope=? AND key=?", (scope, item["key"])).fetchone()
                old = json.loads(row[0]) if row else None
                if not baseline and (old is None or old["fingerprint"] != item["fingerprint"]):
                    kind = item["kind"] + ("_added" if old is None else "_changed")
                    con.execute("INSERT INTO changes(at,kind,course,title,url,before,after) VALUES(?,?,?,?,?,?,?)",
                                (at, kind, item.get("course"), item["title"], item.get("url"),
                                 old.get("due") if old else None, item.get("due")))
                con.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?)", (scope, item["key"], value))
            con.execute("INSERT OR IGNORE INTO scopes VALUES(?)", (scope,))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
            con.execute("DELETE FROM changes WHERE at < ?", (cutoff,))
            con.execute("DELETE FROM changes WHERE id NOT IN (SELECT id FROM changes ORDER BY id DESC LIMIT 5000)")
    finally:
        con.close()


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _file_snapshots(scope: str, rows: list[dict], root: Path) -> None:
    items = []
    cache = _connect()
    try:
        known = {r[0]: json.loads(r[1]) for r in cache.execute("SELECT key,value FROM snapshots WHERE scope=?", (scope,))}
    finally:
        cache.close()
    for row in rows:
        p = root / row["path"]
        if not p.is_file() or not p.resolve().is_relative_to(root.resolve()):
            continue
        stat = p.stat()
        stamp = [str(p), stat.st_mtime_ns, stat.st_size]
        previous = known.get(row["key"], {})
        digest = previous.get("fingerprint") if previous.get("stamp") == stamp else None
        if digest is None:
            h = hashlib.sha256()
            with p.open("rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(block)
            digest = h.hexdigest()
        items.append({**row, "stamp": stamp, "fingerprint": digest})
    observe(scope, items)


def files(con, cid: int, code: str, root: Path) -> None:
    from .moodlelib import BASE_URL
    items = []
    for row in con.execute("SELECT source,path,title,cmid FROM files WHERE course_id=? AND removed_at IS NULL", (cid,)):
        assign = row["source"].startswith("md:assign:")
        url = f"{BASE_URL}/mod/assign/view.php?id={row['cmid']}" if assign else (
            row["source"] if row["source"].startswith("https://") else None)
        items.append({"key": f"{row['cmid']}:{Path(row['path']).name}", "path": row["path"],
                      "kind": "assignment" if assign else "file", "course": code,
                      "title": row["title"] or Path(row["path"]).name, "url": url})
    _file_snapshots(f"moodle:{cid}", items, root)


def ed_files(con, cid: int, root: Path) -> None:
    rows = con.execute("""SELECT f.id,f.local_path,f.title,f.lesson_id,c.code FROM lesson_files f
                          JOIN courses c ON c.id=f.course_id WHERE f.course_id=? AND f.local_path IS NOT NULL""", (cid,))
    items = [{"key": str(r[0]), "path": r[1], "kind": "file", "course": r[4], "title": r[2] or Path(r[1]).name,
              "url": f"https://edstem.org/au/courses/{cid}/lessons/{r[3]}"} for r in rows]
    _file_snapshots(f"ed_files:{cid}", items, root)


def deadlines(items: list[dict]) -> None:
    if load_settings().get("materials_only"):
        return
    # 日历来源间 id 不同，只比较相同来源的快照。
    by_source = {}
    for d in items:
        if d.get("id") is None:
            continue
        by_source.setdefault(d.get("source", "api"), []).append({
            "key": str(d["id"]), "fingerprint": str(d.get("due_ts")), "due": d.get("due"),
            "kind": "deadline", "course": d.get("course"), "title": d.get("activity") or d.get("name") or "截止",
            "url": d.get("url")})
    for source, entries in by_source.items():
        observe("deadlines:" + source, entries)


def staff(conn, thread_id: int, cid: int, title: str, url: str, root_staff: bool, body: str) -> None:
    if load_settings().get("materials_only"):
        return
    row = conn.execute("SELECT code FROM courses WHERE id=?", (cid,)).fetchone()
    code = row[0] if row else str(cid)
    items = [{"key": "root", "fingerprint": fingerprint(body), "kind": "staff_post",
              "course": code, "title": title, "url": url}] if root_staff else []
    for r in conn.execute("SELECT id,text FROM replies WHERE thread_id=? AND is_staff=1", (thread_id,)):
        items.append({"key": str(r[0]), "fingerprint": fingerprint(r[1] or ""), "kind": "staff_reply",
                      "course": code, "title": title, "url": url})
    observe(f"ed:{thread_id}", items)


def recent(since: str | None = None, limit: int = 30) -> list[dict]:
    if not DB.exists():
        return []
    if since is None:
        from .jobs import read_status
        since = read_status().get("sync_started_at") or datetime.now(timezone.utc).isoformat()
    since = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if since.tzinfo is None:
        raise ValueError("变更起始时间必须带时区")
    con = _connect()
    try:
        sql = "SELECT at,kind,course,title,url,before,after FROM changes WHERE at >= ?"
        if load_settings().get("materials_only"):
            sql += " AND (kind LIKE 'file_%' OR kind LIKE 'assignment_%')"
        return [dict(r) for r in con.execute(sql + " ORDER BY id DESC LIMIT ?",
                                             (since.astimezone(timezone.utc).isoformat(), max(1, min(limit, 100))))]
    finally:
        con.close()
