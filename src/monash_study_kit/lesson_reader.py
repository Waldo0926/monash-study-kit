"""整节读 Ed lesson：按页序把文字页、阅读网页、PDF 课件拼成一篇 Markdown，末尾附测验题。

适合“带我过一遍第 3 周的 pre-class”“这节讲了什么”。各部分都是同步时已经存在本地的：
  * document / code 页 → lessons.py 存的 .md（ED_FILES_DIR）
  * webpage 页        → webnotes.py 抓的课程笔记（FILES_DIR，按网址在 web_pages 表里找）；
                        没抓的（需要登录的站、视频之类）只留一个链接
  * pdf 页            → 下载好的 PDF，抽文字，标页码
不发任何网络请求。
"""
from __future__ import annotations

import sqlite3

from . import edquery, textextract
from .moodlelib import FILES_DIR
from .paths import ED_FILES_DIR


def find_lesson(conn, course, lesson) -> dict:
    """lesson 写 id，或标题的一部分（如 "W3 Pre-Class"）。匹配到好几节就列出来让人挑。"""
    rows = edquery.lessons(conn, course)
    ref = str(lesson).strip()
    hits = [l for l in rows if str(l["id"]) == ref] or \
        [l for l in rows if ref.lower() in (l["title"] or "").lower()]
    if not hits:
        titles = "、".join(l["title"] for l in rows[:12]) or "（这门课在 Ed 上没有 Lessons）"
        raise edquery.NotFound(f"{course} 里没有 lesson “{lesson}”。有这些：{titles}")
    exact = [l for l in hits if (l["title"] or "").lower() == ref.lower()]
    if len(hits) > 1 and len(exact) != 1:
        raise edquery.NotFound("匹配到好几节：" + "；".join(f"{l['id']} {l['title']}" for l in hits[:8])
                               + "。写得更具体一点，或者用 id")
    return exact[0] if len(hits) > 1 else hits[0]


def _web_page(url: str) -> str | None:
    """课程笔记里抓好的那份网页（在 moodle.db 的 web_pages 表）。"""
    from . import webnotes
    from .moodlelib import db_connect
    u = webnotes.normalize(url or "")
    if not u:
        return None
    try:
        con = db_connect()
        row = con.execute("SELECT path FROM web_pages WHERE url=? AND path IS NOT NULL", (u,)).fetchone()
        con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        return (FILES_DIR / row["path"]).read_text(encoding="utf-8")
    except OSError:
        return None


def _strip_header(text: str) -> str:
    """存下来的 .md 第一行是 “# 标题”，拼的时候标题我们自己写，去掉重复的。"""
    lines = text.strip().splitlines()
    return "\n".join(lines[1:]).strip() if lines and lines[0].startswith("# ") else text.strip()


def lesson_markdown(conn, course, lesson) -> dict:
    l = find_lesson(conn, course, lesson)       # edquery.lessons 里会顺带补上老库的 idx 列
    rows = conn.execute("SELECT id, type, title, file_url, url, local_path FROM lesson_files WHERE lesson_id=? "
                        "ORDER BY COALESCE(idx, 1e9), id", (l["id"],)).fetchall()
    parts, readable = [], 0
    for r in rows:
        title = r["title"] or r["type"]
        if r["type"] in ("document", "code") and r["local_path"]:
            try:
                body = _strip_header((ED_FILES_DIR / r["local_path"]).read_text(encoding="utf-8"))
            except OSError:
                continue
            parts.append(f"## {title}\n\n{body}")
            readable += 1
        elif r["type"] == "webpage":
            page = _web_page(r["url"])
            if page:
                parts.append(f"## {title}\n\n{_strip_header(page)}")
                readable += 1
            else:
                parts.append(f"## {title}\n\n[网页：{r['url']}]（没有抓下来：可能要登录，或者还没同步到）")
        elif r["type"] == "pdf" and r["local_path"]:
            pages = textextract.pdf_pages(ED_FILES_DIR / r["local_path"])
            text = "\n\n".join(f"[第 {i} 页] {' '.join(p.split())}" for i, p in enumerate(pages, 1) if p.strip())
            parts.append(f"## {title}（PDF）\n\n{text or '（这个 PDF 抽不出文字，可能是图片）'}")
            readable += bool(text)
    quiz = [q for q in edquery.quizzes(conn, course) if q["url"] == l["url"]]
    md = [f"# {l['title']}", f"{l['module'] or ''} · {l['status']} · {l['url']}", ""]
    md.append("\n\n---\n\n".join(parts) if parts else "（这节没有能读的内容，可能全是视频或测验）")
    if quiz:
        md += ["", "---", "", edquery.quiz_markdown(quiz)]
    return {"id": l["id"], "title": l["title"], "status": l["status"], "url": l["url"],
            "readings": readable, "markdown": "\n".join(md).rstrip() + "\n"}
