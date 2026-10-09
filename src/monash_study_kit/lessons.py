"""Ed 课程内容（Lessons）抓取和存储，独立于 threads/replies 那一套。

表结构用 CREATE TABLE IF NOT EXISTS 追加在同一个 ed.db 里。课件 PDF 下载到 ED_FILES_DIR，
全文索引（content_index）会一起索引。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import urllib.request

from .edlib import UA, EdClient, doc_to_text, now_iso
from .paths import ED_FILES_DIR as FILES_DIR
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|]+')


def _safe_name(name: str) -> str:
    name = _UNSAFE_NAME.sub("_", name or "file").strip() or "file"
    return name[:120]


def download_pdf(url: str, dest, log=print) -> bool:
    """下载一个 PDF。已经存在就跳过，不重复下载。file_url 是 Ed 那边签发的直链，
    不用带 x-token（这个域名不是 edstem.org，带了反而可能被拒）。

    先写到 .part 再改名：下到一半断网的话，留下的半个文件不会被当成“已经下好了”。"""
    if dest.exists():
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        req = urllib.request.Request(url, headers={"user-agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            while chunk := resp.read(1 << 16):
                f.write(chunk)
        os.replace(tmp, dest)
        return True
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        log(f"  Ed 课件下载失败 {dest.name}: {e}")
        return False

LESSONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS modules (
    id INTEGER PRIMARY KEY, course_id INTEGER, name TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY, course_id INTEGER, module_id INTEGER, idx INTEGER,
    kind TEXT, type TEXT, title TEXT, status TEXT, slide_count INTEGER,
    is_hidden INTEGER, is_timed INTEGER, timer_duration INTEGER,
    due_at TEXT, effective_due_at TEXT, last_synced TEXT
);
CREATE INDEX IF NOT EXISTS idx_lessons_course ON lessons(course_id, module_id);
CREATE TABLE IF NOT EXISTS lesson_files (
    id INTEGER PRIMARY KEY, lesson_id INTEGER, course_id INTEGER, type TEXT,
    title TEXT, file_url TEXT, url TEXT, local_path TEXT, idx INTEGER   -- idx：slide 在 lesson 里的顺序
);
CREATE INDEX IF NOT EXISTS idx_lesson_files_lesson ON lesson_files(lesson_id);
-- lesson 里 quiz 页的题目和选项（Markdown）。Ed 不给学生看答案（release_quiz_solutions 都是 false），
-- 作答记录的接口也是空的，所以只有题面，拿来当复习清单。
CREATE TABLE IF NOT EXISTS quiz_questions (
    id INTEGER PRIMARY KEY, slide_id INTEGER, lesson_id INTEGER, course_id INTEGER,
    slide_title TEXT, idx INTEGER, type TEXT, multiple INTEGER, question TEXT, options TEXT,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_quiz_lesson ON quiz_questions(lesson_id, slide_id, idx);
"""


def ensure_lessons_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(LESSONS_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lesson_files)")}
    if "idx" not in cols:                       # 0.3 以前的库没有页序，下次同步会补上
        conn.execute("ALTER TABLE lesson_files ADD COLUMN idx INTEGER")


def fetch_lessons(client: EdClient, course_id: int) -> tuple[list[dict], list[dict]]:
    """对应 GET /courses/{id}/lessons，跟 threads() 一样直接用 client.get()。"""
    data = client.get(f"/courses/{course_id}/lessons")
    modules = sorted(data.get("modules", []), key=lambda m: m["id"])
    lessons = [l for l in data.get("lessons", []) if not l.get("is_hidden")]
    lessons.sort(key=lambda l: (l.get("index") is None, l.get("index") or 0))
    return modules, lessons


def fetch_slides(client: EdClient, lesson_id: int) -> list[dict] | None:
    """对应 GET /lessons/{id}，跟 ed-web 里 fetchLessonFiles() 用的是同一个接口。
    列表接口（/courses/{id}/lessons）不带 slides，pdf 的 file_url / webpage 的 url
    都要进这个详情接口才有。

    请求失败返回 None（不是空列表）：调用方据此保留上次存的文件和测验题，
    不然一次网络抖动就会把这节 lesson 的课件清单和题目全删掉。"""
    try:
        data = client.get(f"/lessons/{lesson_id}")
        return [s for s in (data.get("lesson") or {}).get("slides", []) if not s.get("is_hidden")]
    except Exception as e:
        if type(e).__name__ == "EdAuthError":
            raise
        return None


def fetch_lesson_files(client: EdClient, lesson_id: int, slides: list[dict] | None = None) -> list[dict]:
    if slides is None:
        slides = fetch_slides(client, lesson_id) or []
    out = []
    for pos, s in enumerate(slides):
        idx = s.get("index") if isinstance(s.get("index"), int) else pos
        if s.get("type") == "pdf" and s.get("file_url"):
            out.append({"id": s["id"], "type": "pdf", "title": s.get("title") or "",
                        "file_url": s["file_url"], "url": None, "idx": idx})
        elif s.get("type") == "webpage" and s.get("url"):
            out.append({"id": s["id"], "type": "webpage", "title": s.get("title") or "",
                        "file_url": None, "url": s["url"], "idx": idx})
        elif s.get("type") in ("document", "code") and (text := slide_text(s)):
            # 正文直接写在 Ed 里的页：存成 .md，全文索引就能搜到
            out.append({"id": s["id"], "type": s["type"], "title": s.get("title") or "",
                        "file_url": None, "url": None, "text": text, "idx": idx})
    return out


def slide_text(slide: dict) -> str:
    """document / code 页的正文（字段位置不固定：content、passage，或者包在 data 里）。"""
    data = slide.get("data") if isinstance(slide.get("data"), dict) else {}
    raw = slide.get("content") or slide.get("passage") or data.get("content") or data.get("passage") or ""
    text = doc_to_text(raw) if isinstance(raw, str) else ""
    return text if len(text.strip()) >= 40 else ""      # 只有一个链接的页没什么可搜的


def sync_quizzes(conn: sqlite3.Connection, client: EdClient, course_id: int, lesson_id: int,
                 slides: list[dict], log=print) -> int:
    """quiz 页的题目。已经存过的 quiz 页不再重抓（题目基本不改，每小时抓一遍太浪费）；
    lesson 里删掉的 quiz 页，题目跟着删。返回新抓的题数。"""
    quiz = {s["id"]: s for s in slides if s.get("type") == "quiz"}
    have = {r[0] for r in conn.execute("SELECT DISTINCT slide_id FROM quiz_questions WHERE lesson_id=?", (lesson_id,))}
    for gone in have - set(quiz):
        conn.execute("DELETE FROM quiz_questions WHERE slide_id=?", (gone,))
    n = 0
    for sid, s in quiz.items():
        if sid in have:
            continue
        try:
            qs = client.get(f"/lessons/slides/{sid}/questions").get("questions", [])
        except Exception as e:
            if type(e).__name__ == "EdAuthError":
                raise
            log(f"  quiz {sid} 取题失败: {e}")
            continue
        ts = now_iso()
        for q in qs:
            d = q.get("data") or {}
            conn.execute("INSERT OR REPLACE INTO quiz_questions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                q["id"], sid, lesson_id, course_id, s.get("title"), q.get("index"), d.get("type"),
                int(bool(d.get("multiple_selection"))), doc_to_text(d.get("content")),
                json.dumps([doc_to_text(a) for a in d.get("answers") or []], ensure_ascii=False), ts))
            n += 1
    return n


def prune_lessons(conn: sqlite3.Connection, course_id: int, keep: set[int]) -> int:
    """Ed 上删掉或藏起来的 lesson，连同它的课件记录、测验题和下载下来的文件一起去掉。返回删了几节。"""
    gone = [r[0] for r in conn.execute("SELECT id FROM lessons WHERE course_id=?", (course_id,))
            if r[0] not in keep]
    for lid in gone:
        conn.execute("DELETE FROM lesson_files WHERE lesson_id=?", (lid,))
        conn.execute("DELETE FROM quiz_questions WHERE lesson_id=?", (lid,))
        conn.execute("DELETE FROM lessons WHERE id=?", (lid,))
        shutil.rmtree(FILES_DIR / str(course_id) / str(lid), ignore_errors=True)   # 全文索引下次就不再搜到
    return len(gone)


def sync_lessons(conn: sqlite3.Connection, client: EdClient, course_id: int, log=print) -> tuple[int, int]:
    """全量覆盖这门课的 modules/lessons。数据量小（一门课几十条），不用跟 threads
    那样做增量，Ed 的 lesson.updated_at 目前观察下来基本不维护，没法当指纹用。"""
    ensure_lessons_schema(conn)
    modules, lessons = fetch_lessons(client, course_id)
    ts = now_iso()
    prune_lessons(conn, course_id, {l["id"] for l in lessons})

    for m in modules:
        conn.execute(
            "INSERT OR REPLACE INTO modules (id, course_id, name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (m["id"], course_id, m.get("name"), m.get("created_at")),
        )
    for l in lessons:
        conn.execute(
            "INSERT OR REPLACE INTO lessons "
            "(id, course_id, module_id, idx, kind, type, title, status, slide_count, "
            " is_hidden, is_timed, timer_duration, due_at, effective_due_at, last_synced) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                l["id"], course_id, l.get("module_id"), l.get("index"),
                l.get("kind"), l.get("type"), l.get("title"), l.get("status"),
                l.get("slide_count"), int(bool(l.get("is_hidden"))),
                int(bool(l.get("is_timed"))), l.get("timer_duration"),
                l.get("due_at"), l.get("effective_due_at"), ts,
            ),
        )
        slides = fetch_slides(client, l["id"])
        if slides is None:
            log(f"  lesson {l['id']} 的内容没取到，先保留上次的")
            continue
        conn.execute("DELETE FROM lesson_files WHERE lesson_id = ?", (l["id"],))
        sync_quizzes(conn, client, course_id, l["id"], slides, log)
        for f in fetch_lesson_files(client, l["id"], slides):
            local_path = None
            if f["type"] == "pdf" and f["file_url"]:
                fname = f"{f['id']}_{_safe_name(f['title'] or 'slides')}.pdf"
                rel = f"{course_id}/{l['id']}/{fname}"
                if download_pdf(f["file_url"], FILES_DIR / rel, log):
                    local_path = rel
            elif f.get("text"):
                rel = f"{course_id}/{l['id']}/{f['id']}_{_safe_name(f['title'] or f['type'])}.md"
                dest = FILES_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                body = f"# {l.get('title')} · {f['title']}\n\n{f['text']}\n"
                if not dest.exists() or dest.read_text(encoding="utf-8") != body:
                    dest.write_text(body, encoding="utf-8")
                local_path = rel
            conn.execute(
                "INSERT OR REPLACE INTO lesson_files "
                "(id, lesson_id, course_id, type, title, file_url, url, local_path, idx) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f["id"], l["id"], course_id, f["type"], f["title"], f["file_url"], f["url"], local_path,
                 f.get("idx")),
            )
    conn.commit()
    return len(modules), len(lessons)
