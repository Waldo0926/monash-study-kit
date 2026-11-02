"""Ed 课程内容（Lessons）抓取和存储，独立于 threads/replies 那一套。

表结构用 CREATE TABLE IF NOT EXISTS 追加在同一个 ed.db 里。课件 PDF、正文页里附的文件和
文字页下载到 ED_FILES_DIR，按 课号/模块/lesson 分文件夹（跟 Ed 页面上的顺序一致），
全文索引（content_index）会一起索引，edexport 直接把这棵目录打成 zip。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

from .edlib import UA, EdClient, doc_attachments, doc_to_text, now_iso
from .paths import ED_FILES_DIR as FILES_DIR
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|]+')
NAME_MAX = 80                       # 每一级目录/文件名的长度上限：Windows 整条路径不能超过 260
ATTACHMENT_MAX = 200 * 1024 * 1024  # 正文里附的单个文件最多 200 MB，再大的多半是数据集或录像


def _safe_name(name: str) -> str:
    name = _UNSAFE_NAME.sub("_", name or "file").strip() or "file"
    return name[:120]


def _label(name: str | None, fallback: str) -> str:
    """文件夹/文件名里能看懂的标题：“Week 3: Editor/IDE” -> “Week 3 - Editor-IDE”。
    Windows 不允许结尾是点或空格。"""
    s = re.sub(r"\s*:\s*", " - ", (name or "").strip())
    s = re.sub(r"\s+", " ", _UNSAFE_NAME.sub("-", s)).strip(" .")
    return s[:NAME_MAX].rstrip(" .") or fallback


def course_folder(code: str | None, course_id: int) -> str:
    """课号做文件夹名：“FIT2109 S2 2026 Malaysia” -> “FIT2109”，“FIT3162/FIT3164” -> “FIT3162+FIT3164”。"""
    short = (code or "").split()[0] if (code or "").strip() else ""
    return _label(short.replace("/", "+"), str(course_id))


def module_folders(modules: list[dict], lessons: list[dict]) -> dict:
    """module_id -> “03 Week 3 - Editor-Modern IDE”。编号按模块里第一节 lesson 在 Ed 上的位置排，
    这样 Week 10 不会排到 Week 2 前面；不属于任何模块的放在最后的 “Other”。"""
    first: dict = {}
    for pos, l in enumerate(lessons):
        first.setdefault(l.get("module_id"), pos)
    names = {m["id"]: m.get("name") for m in modules}
    order = sorted((mid for mid in first if mid in names), key=first.get)
    out = {mid: f"{n:02d} {_label(names[mid], f'Module {n}')}" for n, mid in enumerate(order, 1)}
    out[None] = f"{len(order) + 1:02d} Other"
    return out


def lesson_folder(lesson: dict, pos: int) -> str:
    n = lesson["index"] + 1 if isinstance(lesson.get("index"), int) else pos + 1
    fallback = f"Lesson {lesson['id']}"
    return f"{n:02d} {_label(lesson.get('title'), fallback)}"


def _file_name(idx: int, title: str | None, fallback: str, ext: str) -> str:
    """“03 Workshop 3 Slides.pdf”：前面是这页在 lesson 里的顺序。标题本来就带扩展名的不重复加。"""
    base = _label(title, fallback)
    if ext and base.lower().endswith(ext.lower()):
        base = base[: -len(ext)].rstrip(" .") or fallback
    return f"{idx + 1:02d} {base[:NAME_MAX]}{ext}"


def attachment_id(url: str) -> int:
    """正文页附件在 lesson_files 里的 id。Ed 的 slide id 都是正数，附件用 URL 算一个固定的负数，
    不会和它们撞，每次同步也一样（下次能找回上次下好的文件）。"""
    return -int.from_bytes(hashlib.sha256(url.encode()).digest()[:7], "big") - 1


def is_ed_file_url(url: str) -> bool:
    """只下载 Ed 自己存的附件（static.edusercontent.com 这类直链），正文里别的外链不碰。"""
    u = urllib.parse.urlsplit(url or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "edusercontent.com" or host.endswith(".edusercontent.com")) \
        and not u.username and not u.port


def download_pdf(url: str, dest, log=print, force: bool = False, max_bytes: int | None = None) -> bool:
    """下载一个课件文件。已经存在就跳过，不重复下载（force：Ed 上换了文件，重新下）。
    file_url 是 Ed 那边签发的直链，不用带 x-token（这个域名不是 edstem.org，带了反而可能被拒）。

    先写到 .part 再改名：下到一半断网的话，留下的半个文件不会被当成“已经下好了”。"""
    if dest.exists() and not force:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        req = urllib.request.Request(url, headers={"user-agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            size = 0
            while chunk := resp.read(1 << 16):
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise ValueError(f"超过 {max_bytes // (1024 * 1024)} MB，跳过")
                f.write(chunk)
        os.replace(tmp, dest)
        return True
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        log(f"  Ed 课件下载失败 {dest.name}: {e}")
        return False


def _move_old(old: str | None, new: str) -> None:
    """老版本按数字 id 存（课程id/lessonid/slideid_标题.pdf），lesson 改名后路径也会变：
    把上次下好的文件挪到新位置，不用重新下载。"""
    if not old or old == new:
        return
    src, dest = FILES_DIR / old, FILES_DIR / new
    if src.is_file() and not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)


def _remove_empty_dirs(start: Path) -> None:
    """删掉文件后，往上把空了的文件夹也删掉，停在 FILES_DIR。"""
    root = FILES_DIR.resolve()
    d = start.resolve()
    while d != root and root in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def _unlink(rel: str) -> None:
    path = FILES_DIR / rel
    path.unlink(missing_ok=True)
    _remove_empty_dirs(path.parent)

LESSONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ed_assets (
    url TEXT PRIMARY KEY, local_path TEXT, sha TEXT, error TEXT, fetched_at TEXT
);
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
        elif s.get("type") in ("document", "code"):
            if text := slide_text(s):
                # 正文直接写在 Ed 里的页：存成 .md，全文索引就能搜到
                out.append({"id": s["id"], "type": s["type"], "title": s.get("title") or "",
                            "file_url": None, "url": None, "text": text, "idx": idx})
            if s.get("type") == "code" and s.get("challenge_id"):
                # 右侧工作区（scaffold、task.txt）只能“连接”Ed 工作区才读得到（会创建工作区），
                # 为了只读不去连，只记下来，导出的索引里说明
                out.append({"id": attachment_id(f"challenge:{s['challenge_id']}"), "type": "challenge",
                            "title": s.get("title") or "", "file_url": None, "url": None, "idx": idx})
            # 正文里附的文件（starter code 的 zip、讲义、数据表）：跟 Ed Web 一样一起下载
            seen = set()
            for a in doc_attachments(_slide_raw(s)):
                if a["kind"] == "file" and is_ed_file_url(a["url"]) and a["url"] not in seen:
                    seen.add(a["url"])
                    out.append({"id": attachment_id(a["url"]), "type": "attachment", "title": a["name"],
                                "file_url": a["url"], "url": None, "idx": idx})
    return out


def _slide_raw(slide: dict) -> str:
    data = slide.get("data") if isinstance(slide.get("data"), dict) else {}
    raw = slide.get("content") or slide.get("passage") or data.get("content") or data.get("passage") or ""
    return raw if isinstance(raw, str) else ""


def slide_text(slide: dict) -> str:
    """document / code 页的正文（字段位置不固定：content、passage，或者包在 data 里）。"""
    raw = _slide_raw(slide)
    text = doc_to_text(raw) if raw else ""
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
        for (rel,) in conn.execute("SELECT local_path FROM lesson_files WHERE lesson_id=? AND local_path IS NOT NULL",
                                   (lid,)).fetchall():
            _unlink(rel)                                    # 全文索引下次就不再搜到
        conn.execute("DELETE FROM lesson_files WHERE lesson_id=?", (lid,))
        conn.execute("DELETE FROM quiz_questions WHERE lesson_id=?", (lid,))
        conn.execute("DELETE FROM lessons WHERE id=?", (lid,))
        shutil.rmtree(FILES_DIR / str(course_id) / str(lid), ignore_errors=True)   # 老版本的目录布局
    return len(gone)


def sync_lessons(conn: sqlite3.Connection, client: EdClient, course_id: int, log=print) -> tuple[int, int]:
    """全量覆盖这门课的 modules/lessons。数据量小（一门课几十条），不用跟 threads
    那样做增量，Ed 的 lesson.updated_at 目前观察下来基本不维护，没法当指纹用。

    文件存在 课号/模块/lesson/ 下面，名字前面带 Ed 上的顺序号，和 Ed 页面一样排。"""
    ensure_lessons_schema(conn)
    repair_image_names(conn)
    modules, lessons = fetch_lessons(client, course_id)
    ts = now_iso()
    prune_lessons(conn, course_id, {l["id"] for l in lessons})
    row = conn.execute("SELECT code FROM courses WHERE id=?", (course_id,)).fetchone()
    cdir = course_folder(row[0] if row else None, course_id)
    mdirs = module_folders(modules, lessons)

    for m in modules:
        conn.execute(
            "INSERT OR REPLACE INTO modules (id, course_id, name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (m["id"], course_id, m.get("name"), m.get("created_at")),
        )
    for pos, l in enumerate(lessons):
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
        before = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT id, local_path, COALESCE(file_url, url) FROM lesson_files WHERE lesson_id = ?", (l["id"],))}
        conn.execute("DELETE FROM lesson_files WHERE lesson_id = ?", (l["id"],))
        sync_quizzes(conn, client, course_id, l["id"], slides, log)
        prefetch_quiz_images(conn, course_id, l["id"], cdir, log)
        ldir = f"{cdir}/{mdirs.get(l.get('module_id'), mdirs[None])}/{lesson_folder(l, pos)}"
        kept = set()
        for f in fetch_lesson_files(client, l["id"], slides):
            local_path = None
            old_path, old_url = before.get(f["id"], (None, None))
            if f["type"] in ("pdf", "attachment") and f["file_url"]:
                if f["type"] == "pdf":
                    fname = _file_name(f["idx"], f["title"], "Slides", ".pdf")
                else:
                    ext = Path(f["title"]).suffix if 1 < len(Path(f["title"]).suffix) <= 10 else ""
                    fname = _file_name(f["idx"], f["title"], "Attachment", ext)
                rel = f"{ldir}/{fname}"
                if rel in kept:                 # 同一页附了两个同名文件
                    stem, ext = os.path.splitext(fname)
                    rel = f"{ldir}/{stem} ({abs(f['id']) % 10000}){ext}"
                _move_old(old_path, rel)
                # Ed 上换了文件，file_url 也会换（每次上传一个新链接）：重新下载
                changed = bool(old_url) and old_url != f["file_url"]
                if download_pdf(f["file_url"], FILES_DIR / rel, log, force=changed,
                                max_bytes=ATTACHMENT_MAX if f["type"] == "attachment" else None):
                    local_path = rel
            elif f["type"] == "webpage" and old_path and old_url == f["url"]:
                # Google 导出的网页文件保留到下一轮，避免每次 Lessons 同步都删掉重下。
                if (FILES_DIR / old_path).is_file():
                    local_path = old_path
            elif f.get("text"):
                rel = f"{ldir}/{_file_name(f['idx'], f['title'], f['type'].title(), '.md')}"
                _move_old(old_path, rel)
                dest = FILES_DIR / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                text = localize_images(conn, f["text"], cdir, dest.parent, log)
                body = f"# {l.get('title')} · {f['title']}\n\n{text}\n"
                if not dest.exists() or dest.read_text(encoding="utf-8") != body:
                    dest.write_text(body, encoding="utf-8")
                local_path = rel
            if local_path:
                kept.add(local_path)
            conn.execute(
                "INSERT OR REPLACE INTO lesson_files "
                "(id, lesson_id, course_id, type, title, file_url, url, local_path, idx) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f["id"], l["id"], course_id, f["type"], f["title"], f["file_url"], f["url"], local_path,
                 f.get("idx")),
            )
        # Ed 上删掉的页、改名前的旧位置：本地文件也去掉
        for old_path, _ in before.values():
            if old_path and old_path not in kept:
                _unlink(old_path)
    conn.commit()
    return len(modules), len(lessons)


IMAGE_EXT = r"\.(png|jpe?g|gif|webp|svg|bmp)"
_SIGNATURES = ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"), (b"BM", ".bmp"))


def image_ext(head: bytes) -> str | None:
    """按文件开头几个字节认出图片格式；认不出返回 None。"""
    for sig, ext in _SIGNATURES:
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    text = head.lstrip().lower()
    if text.startswith(b"<svg") or text.startswith(b"<?xml") and b"<svg" in text:
        return ".svg"
    return None


def repair_image_names(conn) -> int:
    """1.10.0 及以前，没有扩展名的 Ed 图片存成了 .img（查看器不显示）：按内容改成 .png / .jpg …，
    各门课 assets/ 里的副本和 .md 里的引用一起改。返回改了几个。"""
    renames = {}
    for url, local in conn.execute("SELECT url, local_path FROM ed_assets WHERE local_path LIKE '%.img'").fetchall():
        src = FILES_DIR / local
        ext = image_ext(src.read_bytes()[:4096]) if src.is_file() else None
        if not ext:
            continue
        renames[src.name] = src.stem + ext
        conn.execute("UPDATE ed_assets SET local_path=? WHERE url=?", (Path(local).with_suffix(ext).as_posix(), url))
    if not renames:
        return 0
    for old, new in renames.items():
        for p in FILES_DIR.glob(f"*/assets/{old}"):
            p.replace(p.with_name(new))
    for md in FILES_DIR.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        fixed = text
        for old, new in renames.items():
            fixed = fixed.replace(f"assets/{old})", f"assets/{new})")
        if fixed != text:
            md.write_text(fixed, encoding="utf-8")
    conn.commit()
    return len(renames)


def localize_images(conn, text: str, cdir: str, md_dir: Path, log=print) -> str:
    """只存 Ed 自己的正文图片，缓存按 URL 查、文件按内容去重。"""
    def image(match):
        url = match[2]
        if not is_ed_file_url(url):
            return match[0]
        row = conn.execute("SELECT local_path, error FROM ed_assets WHERE url=?", (url,)).fetchone()
        if row and row[0] and (FILES_DIR / row[0]).is_file():
            path = FILES_DIR / row[0]
        else:
            tmp = FILES_DIR / cdir / "assets" / (hashlib.sha256(url.encode()).hexdigest() + ".download")
            errors = []
            if not download_pdf(url, tmp, errors.append, max_bytes=ATTACHMENT_MAX):
                conn.execute("INSERT OR REPLACE INTO ed_assets VALUES (?,NULL,NULL,?,?)",
                             (url, "图片下载失败或超过 200 MB", now_iso()))
                return match[0]
            sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
            ext = Path(urllib.parse.urlsplit(url).path).suffix.lower()
            if not re.fullmatch(IMAGE_EXT, ext):
                # Ed 的图片地址没有扩展名：按文件头判断，不然查看器不会把它当图片显示
                ext = image_ext(tmp.read_bytes()[:4096]) or ".img"
            path = next(iter(sorted(tmp.parent.glob(sha[:16] + ".*"))), tmp.parent / (sha[:16] + ext))
            os.replace(tmp, path)
            conn.execute("INSERT OR REPLACE INTO ed_assets VALUES (?,?,?,NULL,?)",
                         (url, path.relative_to(FILES_DIR).as_posix(), sha, now_iso()))
        local = FILES_DIR / cdir / "assets" / path.name
        if path != local:
            local.parent.mkdir(parents=True, exist_ok=True)
            if not local.exists():
                shutil.copy2(path, local)
            path = local
        rel = os.path.relpath(path, md_dir).replace(os.sep, "/")
        return f"![{match[1]}]({rel})"
    return re.sub(r"!\[([^\]]*)\]\((https://[^\s)]+)\)", image, text)


def prefetch_quiz_images(conn, course_id: int, lesson_id: int, cdir: str, log=print) -> None:
    """测验题目和选项里的 Ed 图片先下载到 assets/（Quiz.md 是导出时现生成的，导出时再换成相对路径）。"""
    for q, opts in conn.execute("SELECT question, options FROM quiz_questions WHERE lesson_id=?", (lesson_id,)).fetchall():
        for text in [q or "", *json.loads(opts or "[]")]:
            if "edusercontent.com" in text:
                localize_images(conn, text, cdir, FILES_DIR / cdir, log)


def asset_paths(conn) -> dict[str, str]:
    """已经下载好的 Ed 图片：远程地址 -> assets/ 里的文件名。导出时把 Quiz.md 里的地址换掉用。"""
    out = {}
    for url, local in conn.execute("SELECT url, local_path FROM ed_assets WHERE local_path IS NOT NULL"):
        if (FILES_DIR / local).is_file():
            out[url] = Path(local).name
    return out
