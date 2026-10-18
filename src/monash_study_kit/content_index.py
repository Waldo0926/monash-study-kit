"""课件全文索引：PDF、录播字幕稿、网页/文本课件，用 SQLite FTS5 搜。

    monash grep "monad"               # 搜，结果带页码 / 字幕时间戳（sync 完会自动增量建索引）
    monash grep "docker volume" FIT2109

索引两处的文件：
  - Moodle 同步下来的课件（FILES_DIR），课号取自第一层目录名；
  - Ed Lessons 的课件 PDF 和文字页（ED_FILES_DIR），课号和 lesson 标题查 ed.db。

切块：字幕稿按 **[MM:SS]** 段落，PDF 按页，docx/pptx 按段/页，其他文本按空行攒到约 1500 字。
zip 里的文本文件（starter code、README）也索引，loc 是 zip 内的路径，交给 read_file 的 inner。
每块一行，loc 记“p.3”“12:34”“slide 5”这类定位。文件没变（mtime + size）不重新抽取。
视频不索引。
"""
from __future__ import annotations

import re
import sqlite3
import zipfile
from pathlib import Path

from . import moodlelib, textextract
from .edlib import DB_PATH as ED_DB
from .moodlelib import FILES_DIR, now_iso
from .paths import ED_FILES_DIR
TEXT_EXT = {".md", ".txt", ".html", ".htm", ".py", ".hs", ".java", ".c", ".h", ".js", ".ts", ".sh",
            ".json", ".csv", ".ipynb", ".r", ".sql", ".tex", ".yaml", ".yml"}
INDEX_EXT = TEXT_EXT | {".pdf", ".docx", ".pptx", ".zip"}
ZIP_MEMBER_MAX = 1 << 20      # zip 里单个文本文件最多读 1 MB，再大的多半是数据集
ZIP_MEMBERS_MAX = 300
ED_PREFIX = "ed:"             # Ed 的文件在 read_file 里带这个前缀
CHUNK = 1500
MAX_FILE = 30 * 1024 * 1024
CODE_RE = re.compile(r"\b([A-Z]{3}\d{4})\b")
TS_RE = re.compile(r"^\*\*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\*\*\s*", re.M)

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_files (
    path TEXT PRIMARY KEY, source TEXT, course TEXT, title TEXT,
    mtime REAL, size INTEGER, chunks INTEGER, indexed_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    text, path UNINDEXED, loc UNINDEXED, tokenize = 'porter unicode61'
);
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # 1.0.3 以前的库没有 url（Ed 课件的原文链接），补上
    if "url" not in {r[1] for r in con.execute("PRAGMA table_info(content_files)")}:
        con.execute("ALTER TABLE content_files ADD COLUMN url TEXT")


# ---------------------------------------------------------------- 抽文字

def _paragraph_chunks(text: str, prefix: str = "¶") -> list[tuple[str, str]]:
    out, buf, n = [], [], 0
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        buf.append(para)
        if sum(len(p) for p in buf) >= CHUNK:
            n += 1
            out.append((f"{prefix}{n}", "\n\n".join(buf)))
            buf = []
    if buf:
        out.append((f"{prefix}{n + 1}", "\n\n".join(buf)))
    return out


def transcript_chunks(text: str) -> list[tuple[str, str]]:
    """**[01:06]** 开头的段落各成一块，loc 就是时间戳。"""
    parts = TS_RE.split(text)
    # split 结果：[开头, ts1, 正文1, ts2, 正文2, ...]
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2) if parts[i + 1].strip()]


def pdf_chunks(path: Path) -> list[tuple[str, str]]:
    pages = textextract.pdf_pages(path)
    return [(f"p.{i}", " ".join(p.split())) for i, p in enumerate(pages, 1) if p.strip()]


def office_chunks(path: Path) -> list[tuple[str, str]]:
    parts = textextract.office_parts(path)
    if path.suffix.lower() == ".pptx":
        return parts
    return _paragraph_chunks(parts[0][1]) if parts else []


def zip_chunks(path: Path) -> list[tuple[str, str]]:
    """zip 里的文本文件按空行切块，loc 是 zip 内路径（read_file 的 inner 直接用它）。"""
    out = []
    with zipfile.ZipFile(path) as z:
        members = [i for i in z.infolist() if not i.is_dir() and i.file_size <= ZIP_MEMBER_MAX
                   and Path(i.filename).suffix.lower() in TEXT_EXT
                   and not i.filename.startswith("__MACOSX/") and not Path(i.filename).name.startswith(".")]
        for info in members[:ZIP_MEMBERS_MAX]:
            text = z.read(info).decode("utf-8", "replace")
            out += [(info.filename, chunk) for _, chunk in _paragraph_chunks(text)]
    return out


def extract(path: Path) -> list[tuple[str, str]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return pdf_chunks(path)
    if ext == ".zip":
        try:
            return zip_chunks(path)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError):  # 坏包、少见的压缩方式、加密
            return []
    if ext in (".docx", ".pptx"):
        try:
            return office_chunks(path)
        except (zipfile.BadZipFile, KeyError):
            return []
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.name.endswith(".transcript.md"):
        return transcript_chunks(text)
    if ext in (".html", ".htm"):
        text = re.sub(r"(?s)<(script|style).*?</\1>|<[^>]+>", " ", text)
    return _paragraph_chunks(text)


# ---------------------------------------------------------------- 要索引哪些文件

def _ed_labels() -> dict[str, tuple[str, str, str | None]]:
    """Ed 课件的 local_path → (课号, "lesson 标题 · 文件标题", 原文链接)。

    两处来源：lesson_files（课件 PDF）和 lesson_texts（Ed 文字页、抓下来的阅读网页；
    ed-digest 的库有这张表，本包自己同步的库没有，没有就跳过）。"""
    db = ED_DB
    if not db.exists():
        return {}
    rows = []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(lesson_files)")}
        url = "f.file_url" if "file_url" in cols else "NULL"
        rows += con.execute(f"""SELECT f.local_path, f.title, l.title, c.code, {url} FROM lesson_files f
                                JOIN lessons l ON l.id = f.lesson_id JOIN courses c ON c.id = f.course_id
                                WHERE f.local_path IS NOT NULL""").fetchall()
        try:
            rows += con.execute("""SELECT t.local_path, t.title, l.title, c.code,
                                          COALESCE(t.url, 'https://edstem.org/au/courses/' || t.course_id
                                                          || '/lessons/' || t.lesson_id || '/slides/' || t.slide_id)
                                   FROM lesson_texts t JOIN lessons l ON l.id = t.lesson_id
                                   JOIN courses c ON c.id = t.course_id
                                   WHERE t.local_path IS NOT NULL""").fetchall()
        except sqlite3.OperationalError:
            pass
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    out = {}
    for rel, ftitle, ltitle, code, link in rows:
        m = CODE_RE.search(code or "")
        out[rel] = (m.group(1) if m else (code or "").strip(), f"{ltitle} · {ftitle}", link)
    return out


def _removed() -> set[str]:
    """Moodle 上已经下架的文件，不再进搜索。还没有库（第一次同步前、测试里）就当没有。"""
    if not (moodlelib.DATA_DIR / "moodle.db").exists():
        return set()
    con = moodlelib.db_connect()
    try:
        return moodlelib.removed_paths(con)
    finally:
        con.close()


def _inside(path: Path, root: Path) -> bool:
    """软链接指到文件目录外面的不进索引，和 read_file 的规则一样。"""
    return not path.is_symlink() or root.resolve() in path.resolve().parents


def candidates() -> list[dict]:
    files = []
    if FILES_DIR.exists():
        removed = _removed()
        for p in FILES_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in INDEX_EXT and _inside(p, FILES_DIR):
                rel = p.relative_to(FILES_DIR)
                if rel.as_posix() in removed:
                    continue
                m = CODE_RE.search(rel.parts[0]) if len(rel.parts) > 1 else None
                files.append({"path": str(p), "source": "moodle", "course": m.group(1) if m else None,
                              "title": rel.as_posix(), "url": None})
    ed_files = ED_FILES_DIR
    if ed_files.exists():
        labels = _ed_labels()
        for p in ed_files.rglob("*"):
            if p.is_file() and p.suffix.lower() in INDEX_EXT and _inside(p, ed_files):
                rel = p.relative_to(ed_files).as_posix()
                course, title, url = labels.get(rel, (None, rel, None))
                files.append({"path": str(p), "source": "ed", "course": course, "title": title, "url": url})
    return files


# ---------------------------------------------------------------- 建索引 / 搜

def index(con: sqlite3.Connection, verbose: bool = False) -> dict:
    ensure_schema(con)
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, mtime, size FROM content_files")}
    seen, stats = set(), {"indexed": 0, "unchanged": 0, "removed": 0, "chunks": 0}
    for f in candidates():
        p = Path(f["path"])
        try:
            st = p.stat()
        except OSError:         # 列完目录到这里之间被删了（同步正好在改名）
            continue
        seen.add(f["path"])
        if st.st_size > MAX_FILE:
            continue
        if known.get(f["path"]) == (st.st_mtime, st.st_size):
            # 标签可能变了（比如 ed 那边补了 lesson 标题），便宜，顺手更新
            con.execute("UPDATE content_files SET course=?, title=?, url=? WHERE path=?",
                        (f["course"], f["title"], f["url"], f["path"]))
            stats["unchanged"] += 1
            continue
        try:
            chunks = extract(p)
        except Exception as e:  # noqa: BLE001  一个坏文件（加密的 zip、半截的 docx）不该让整次索引失败
            if verbose:
                print(f"! {p}: {e}")
            continue
        con.execute("DELETE FROM content_fts WHERE path = ?", (f["path"],))
        con.executemany("INSERT INTO content_fts (text, path, loc) VALUES (?,?,?)",
                        [(text, f["path"], loc) for loc, text in chunks])
        con.execute("INSERT OR REPLACE INTO content_files (path, source, course, title, mtime, size, chunks, "
                    "indexed_at, url) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f["path"], f["source"], f["course"], f["title"], st.st_mtime, st.st_size,
                     len(chunks), now_iso(), f["url"]))
        stats["indexed"] += 1
        stats["chunks"] += len(chunks)
        if verbose:
            print(f"  {len(chunks):4d} 块  {f['title']}")
    for gone in set(known) - seen:
        con.execute("DELETE FROM content_fts WHERE path = ?", (gone,))
        con.execute("DELETE FROM content_files WHERE path = ?", (gone,))
        stats["removed"] += 1
    con.commit()
    return stats


def fts_query(q: str) -> str:
    """用户输入 → FTS5 查询：每个词都要出现；"引号里的"当短语；词尾 * 当前缀。其余符号去掉，避免语法错误。"""
    parts = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', q):
        if phrase:
            toks = re.findall(r"\w+", phrase)
            if toks:
                parts.append('"' + " ".join(toks) + '"')
        else:
            star = word.endswith("*")
            for t in re.findall(r"\w+", word):
                parts.append(f'"{t}"' + ("*" if star else ""))
    return " AND ".join(parts)


def search(con: sqlite3.Connection, query: str, course: str | None = None, limit: int = 20) -> list[dict]:
    ensure_schema(con)
    q = fts_query(query)
    if not q:
        return []
    sql = """SELECT f.course, f.source, f.title, t.path, t.loc,
                    snippet(content_fts, 0, '«', '»', '…', 24) AS snip, bm25(content_fts) AS score, f.url
             FROM content_fts t JOIN content_files f ON f.path = t.path
             WHERE content_fts MATCH ?"""
    args: list = [q]
    if course:
        sql += " AND f.course = ?"
        args.append(course.upper())
    sql += " ORDER BY score LIMIT ?"
    args.append(limit)
    out = []
    for r in con.execute(sql, args):
        path = Path(r[3])
        hit = {"course": r[0], "source": r[1], "title": r[2], "loc": r[4],
               "snippet": " ".join(r[5].split()), "path": readable_path(path), "abs_path": r[3], "url": r[7],
               "video": _video_for(path) if path.name.endswith(".transcript.md") else None}
        if path.suffix.lower() == ".zip":
            hit["inner"] = r[4]   # 命中的是 zip 里的这个文件，交给 read_file 的 inner
        out.append(hit)
    return out


def readable_path(path: Path) -> str | None:
    """给 read_file 用的相对路径：Moodle 课件是 FILES_DIR 下的相对路径，Ed 的加 "ed:" 前缀。"""
    if FILES_DIR in path.parents:
        return path.relative_to(FILES_DIR).as_posix()
    if ED_FILES_DIR in path.parents:
        return ED_PREFIX + path.relative_to(ED_FILES_DIR).as_posix()
    return None


def resolve_readable(rel: str) -> Path:
    """readable_path 的反向：相对路径 → 绝对路径，只允许落在两个文件目录里面。"""
    rel = rel.strip()
    base = FILES_DIR
    if rel.startswith(ED_PREFIX):
        base, rel = ED_FILES_DIR, rel[len(ED_PREFIX):]
    path = (base / rel.lstrip("/")).resolve()
    if base.resolve() not in path.parents or not path.is_file():
        raise ValueError("只能读 list_files / search_content 给出的文件（相对路径，Ed 的带 ed: 前缀）")
    return path


def _video_for(transcript: Path) -> str | None:
    base = transcript.name[: -len(".transcript.md")]
    for ext in (".mp4", ".m4a", ".webm", ".mkv"):
        v = transcript.with_name(base + ext)
        if v.exists():
            return v.name
    return None
