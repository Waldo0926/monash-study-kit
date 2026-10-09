"""把 Ed 上跟踪的课同步进本地库：帖子列表 → 变了的帖子抓全文和回复。

增量靠 content_stamp：列表里每帖的 updated_at + 几个计数，变了才重抓正文。
第一次同步是全量，一门课几百帖要几分钟（全局限流 0.25 秒一个请求）。
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

from . import edlib
from .edlib import EdAuthError, EdClient, db_connect, now_iso

# 正文存储格式的版本。改了转换规则就加一，下一次 sync 会把所有帖子重抓一遍。
BODY_FORMAT = "md1"


def _hash(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def content_stamp(t: dict) -> str:
    """帖子的变更指纹：updated_at 再加几个计数和状态位，内容真变了才算变。"""
    parts = (t.get("updated_at"), t.get("reply_count"), t.get("unresolved_count"),
             t.get("vote_count"), t.get("is_staff_answered"), t.get("is_answered"),
             t.get("is_locked"), t.get("title"))
    return _hash(BODY_FORMAT, *parts)


def short_code(code: str | None) -> str:
    """'FIT2109 S2 2026 Malaysia' → 'FIT2109'。"""
    return (code or "").split()[0] if code else ""


def upsert_courses(conn, courses: list[dict]) -> None:
    for c in courses:
        conn.execute(
            "INSERT INTO courses (id, code, name, year, session, status, tracked) VALUES (?,?,?,?,?,?,0) "
            "ON CONFLICT(id) DO UPDATE SET code=excluded.code, name=excluded.name, year=excluded.year, "
            "session=excluded.session, status=excluded.status",
            (c["id"], c["code"], c["name"], c["year"], c["session"], c["status"]))
    conn.commit()


def auto_track(conn, courses: list[dict]) -> list[dict]:
    """第一次用的时候，默认跟踪所有 active 的课（以后可以用 monash courses 改）。"""
    active = [c for c in courses if c.get("status") == "active"]
    for c in active:
        conn.execute("UPDATE courses SET tracked=1 WHERE id=?", (c["id"],))
    conn.commit()
    return active


def store_thread(conn, cid: int, t: dict, full: dict, stamp: str, prev=None) -> tuple[int, int]:
    """把一个帖子（列表项 t + 详情 full）写进库。返回 (是否新帖, 新回复数)。"""
    th = full.get("thread") or {}
    body = edlib.body_of(th)
    url = f"{edlib.WEB_BASE}/{cid}/discussion/{t['id']}"
    is_new = prev is None
    new_replies = 0
    conn.execute("""
        INSERT INTO threads (id, course_id, number, type, title, category, subcategory,
            author, user_id, is_private, is_pinned, is_staff_answered, is_student_answered,
            is_answered, is_endorsed, reply_count, view_count, vote_count,
            created_at, updated_at, body, url, first_seen, last_fetched, content_hash)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, category=excluded.category, subcategory=excluded.subcategory,
            is_pinned=excluded.is_pinned, is_staff_answered=excluded.is_staff_answered,
            is_student_answered=excluded.is_student_answered, is_answered=excluded.is_answered,
            reply_count=excluded.reply_count, view_count=excluded.view_count,
            vote_count=excluded.vote_count, updated_at=excluded.updated_at,
            body=excluded.body, last_fetched=excluded.last_fetched,
            content_hash=excluded.content_hash
    """, (t["id"], cid, t.get("number"), t.get("type"), t.get("title"),
          t.get("category"), t.get("subcategory"), edlib.author_of(t, full.get("users") or {}), t.get("user_id"),
          int(bool(t.get("is_private"))), int(bool(t.get("is_pinned"))),
          int(bool(t.get("is_staff_answered"))), int(bool(t.get("is_student_answered"))),
          int(bool(t.get("is_answered"))), int(bool(t.get("is_endorsed"))),
          t.get("reply_count") or 0, t.get("view_count") or 0, t.get("vote_count") or 0,
          t.get("created_at"), t.get("updated_at"), body, url,
          now_iso(), now_iso(), stamp))

    existing = {r["id"] for r in conn.execute("SELECT id FROM replies WHERE thread_id=?", (t["id"],))}
    flat = edlib.flatten_replies(th, full.get("users") or {})
    edlib.save_attachments(conn, t["id"], th, flat)
    # 拿到的是整帖，库里有、这次没有的回复就是在 Ed 上被删了，别让它一直留在本地
    gone = existing - {r["id"] for r in flat}
    conn.executemany("DELETE FROM replies WHERE id=?", [(rid,) for rid in gone])
    for r in flat:
        if r["id"] not in existing:
            new_replies += 1
        conn.execute("""
            INSERT INTO replies (id, thread_id, parent_id, kind, depth, user_id, author,
                is_endorsed, vote_count, created_at, updated_at, text, first_seen, is_staff)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET text=excluded.text, author=excluded.author,
                is_endorsed=excluded.is_endorsed, vote_count=excluded.vote_count,
                updated_at=excluded.updated_at, is_staff=excluded.is_staff
        """, (r["id"], t["id"], r["parent_id"], r["kind"], r["depth"], r["user_id"],
              r["author"], int(r["is_endorsed"]), r["vote_count"],
              r["created_at"], r["updated_at"], r["text"], now_iso(),
              int(r.get("is_staff", False))))
    return int(is_new), new_replies


def my_user_id(client: EdClient) -> int | None:
    """自己的 Ed user id（判断“我发的帖”用），第一次问 /user 后记下来。"""
    cfg = edlib.load_config()
    if cfg.get("user_id"):
        return cfg["user_id"]
    try:
        uid = (client.me().get("user") or {}).get("id")
    except Exception:  # noqa: BLE001
        return None
    if uid:
        edlib.save_config({**cfg, "user_id": uid})
    return uid


def sync(course_ids: list[int] | None = None, full: bool = False, workers: int = 4, log=print,
         client: EdClient | None = None) -> dict:
    """增量同步跟踪的课（或指定的课）。返回 {new_threads, new_replies}。"""
    client = client or EdClient()
    conn = db_connect()
    courses = client.courses()
    upsert_courses(conn, courses)
    tracked = [r["id"] for r in conn.execute("SELECT id FROM courses WHERE tracked=1")]
    if not tracked and not course_ids:
        tracked = [c["id"] for c in auto_track(conn, courses)]
    course_ids = course_ids or tracked

    started = now_iso()
    total_threads = total_replies = 0
    me = my_user_id(client)
    for cid in course_ids:
        row = conn.execute("SELECT code FROM courses WHERE id=?", (cid,)).fetchone()
        label = short_code(row["code"]) if row else str(cid)
        listing = client.threads(cid)
        edlib.save_thread_state(conn, listing, me)
        known = {r["id"]: r for r in conn.execute(
            "SELECT id, content_hash FROM threads WHERE course_id=?", (cid,))}
        to_fetch = [(t, content_stamp(t)) for t in listing
                    if full or t["id"] not in known or known[t["id"]]["content_hash"] != content_stamp(t)]
        log(f"Ed {label}：{len(listing)} 帖，{len(to_fetch)} 帖要抓")

        def grab(item):
            t, stamp = item
            try:
                return t, stamp, client.thread(t["id"]), None
            except Exception as e:  # noqa: BLE001
                return t, stamp, None, e

        # 网络等待是大头；全局限流在 EdClient 里，并发只是把等待重叠起来，不会加快请求速率
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (t, stamp, data, err) in enumerate(pool.map(grab, to_fetch), 1):
                if err is not None:
                    if isinstance(err, EdAuthError):
                        raise err
                    log(f"  ! 帖子 {t['id']} 抓取失败: {err}")
                    continue
                is_new, n_rep = store_thread(conn, cid, t, data, stamp, known.get(t["id"]))
                total_threads += is_new
                total_replies += n_rep
                if i % 20 == 0:
                    conn.commit()
                    log(f"  … {i}/{len(to_fetch)}")
        conn.commit()

    conn.execute("INSERT INTO runs (started_at, finished_at, new_threads, new_replies, note) VALUES (?,?,?,?,?)",
                 (started, now_iso(), total_threads, total_replies, "full" if full else "incremental"))
    conn.commit()
    conn.close()
    return {"new_threads": total_threads, "new_replies": total_replies}


def refresh_thread(conn, thread_id: int) -> None:
    """从 Ed 重抓一个帖子写回库（ed_thread live=true 用）。"""
    full = EdClient().thread(thread_id)
    th = full.get("thread") or {}
    prev = conn.execute("SELECT content_hash FROM threads WHERE id=?", (thread_id,)).fetchone()
    store_thread(conn, th["course_id"], th, full, content_stamp(th), prev)
    conn.commit()
