"""课件全文索引：PDF、录播字幕稿、网页/文本课件，用 SQLite FTS5 搜。

    monash grep "monad"               # 搜，结果带页码 / 字幕时间戳（sync 完会自动增量建索引）
    monash grep "docker volume" FIT2109

索引两处的文件：
  - Moodle 同步下来的课件（FILES_DIR），课号取自第一层目录名；
  - Ed Lessons 的课件 PDF（ED_FILES_DIR），课号和 lesson 标题查 ed.db。

切块：字幕稿按 **[MM:SS]** 段落，PDF 按页（pypdf），docx/pptx 按段/页，其他文本按空行攒到约 1500 字。
每块一行，loc 记“p.3”“12:34”“slide 5”这类定位。文件没变（mtime + size）不重新抽取。
视频、zip 不索引（zip 里的东西 read_file 能按需读）。
"""
from __future__ import annotations

import re
import sqlite3
import zipfile
from pathlib import Path

from . import textextract
from .edlib import DB_PATH as ED_DB
from .moodlelib import FILES_DIR, now_iso
from .paths import ED_FILES_DIR
TEXT_EXT = {".md", ".txt", ".html", ".htm", ".py", ".hs", ".java", ".c", ".h", ".js", ".ts", ".sh",
            ".json", ".csv", ".ipynb", ".r", ".sql", ".tex", ".yaml", ".yml"}
INDEX_EXT = TEXT_EXT | {".pdf", ".docx", ".pptx"}
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


def extract(path: Path) -> list[tuple[str, str]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return pdf_chunks(path)
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

def _ed_labels() -> dict[str, tuple[str, str]]:
    """Ed 课件的 local_path → (课号, "lesson 标题 · 文件标题")。"""
    db = ED_DB
    if not db.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("""SELECT f.local_path, f.title, l.title, c.code FROM lesson_files f
                              JOIN lessons l ON l.id = f.lesson_id JOIN courses c ON c.id = f.course_id
                              WHERE f.local_path IS NOT NULL""").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    out = {}
    for rel, ftitle, ltitle, code in rows:
        m = CODE_RE.search(code or "")
        out[rel] = (m.group(1) if m else (code or "").strip(), f"{ltitle} · {ftitle}")
    return out


def candidates() -> list[dict]:
    files = []
    if FILES_DIR.exists():
        for p in FILES_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in INDEX_EXT:
                rel = p.relative_to(FILES_DIR)
                m = CODE_RE.search(rel.parts[0]) if len(rel.parts) > 1 else None
                files.append({"path": str(p), "source": "moodle", "course": m.group(1) if m else None,
                              "title": rel.as_posix()})
    ed_files = ED_FILES_DIR
    if ed_files.exists():
        labels = _ed_labels()
        for p in ed_files.rglob("*"):
            if p.is_file() and p.suffix.lower() in INDEX_EXT:
                rel = p.relative_to(ed_files).as_posix()
                course, title = labels.get(rel, (None, rel))
                files.append({"path": str(p), "source": "ed", "course": course, "title": title})
    return files


# ---------------------------------------------------------------- 建索引 / 搜

def index(con: sqlite3.Connection, verbose: bool = False) -> dict:
    ensure_schema(con)
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, mtime, size FROM content_files")}
    seen, stats = set(), {"indexed": 0, "unchanged": 0, "removed": 0, "chunks": 0}
    for f in candidates():
        p = Path(f["path"])
        st = p.stat()
        seen.add(f["path"])
        if st.st_size > MAX_FILE:
            continue
        if known.get(f["path"]) == (st.st_mtime, st.st_size):
            # 标签可能变了（比如 ed 那边补了 lesson 标题），便宜，顺手更新
            con.execute("UPDATE content_files SET course=?, title=? WHERE path=?",
                        (f["course"], f["title"], f["path"]))
            stats["unchanged"] += 1
            continue
        try:
            chunks = extract(p)
        except OSError as e:
            if verbose:
                print(f"! {p}: {e}")
            continue
        con.execute("DELETE FROM content_fts WHERE path = ?", (f["path"],))
        con.executemany("INSERT INTO content_fts (text, path, loc) VALUES (?,?,?)",
                        [(text, f["path"], loc) for loc, text in chunks])
        con.execute("INSERT OR REPLACE INTO content_files VALUES (?,?,?,?,?,?,?,?)",
                    (f["path"], f["source"], f["course"], f["title"], st.st_mtime, st.st_size,
                     len(chunks), now_iso()))
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
                    snippet(content_fts, 0, '«', '»', '…', 24) AS snip, bm25(content_fts) AS score
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
        rel = path.relative_to(FILES_DIR).as_posix() if FILES_DIR in path.parents else None
        out.append({"course": r[0], "source": r[1], "title": r[2], "loc": r[4],
                    "snippet": " ".join(r[5].split()), "path": rel, "abs_path": r[3],
                    "video": _video_for(path) if path.name.endswith(".transcript.md") else None})
    return out


def _video_for(transcript: Path) -> str | None:
    base = transcript.name[: -len(".transcript.md")]
    for ext in (".mp4", ".m4a", ".webm", ".mkv"):
        v = transcript.with_name(base + ext)
        if v.exists():
            return v.name
    return None
