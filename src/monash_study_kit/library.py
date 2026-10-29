"""Loopback-only browser for already synced course files; no login or write routes."""
from __future__ import annotations

import html
import shutil
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import content_index
from .paths import DATA_DIR, ED_DB

STYLE = """body{margin:0;background:#f5f5f2;color:#20251f;font:16px/1.6 system-ui,sans-serif}
main{max-width:1040px;margin:auto;padding:40px 24px}header{border-bottom:1px solid #d8ddd5;margin-bottom:28px}
h1{font-size:36px;letter-spacing:-1px;margin:0}p,small{color:#596253}form{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}
input,button{font:inherit;border:1px solid #ccd3c7;border-radius:8px;padding:10px 14px;background:white}
input{flex:1;min-width:160px}button{cursor:pointer;background:#1e4937;color:white}ul{padding:0;list-style:none}
li{background:white;border:1px solid #dbe0d7;border-radius:10px;padding:16px;margin:10px 0;overflow-wrap:anywhere}
a{color:#1b573c}small{display:block}h2{font-size:20px;margin-top:30px}.empty{padding:32px;background:white;border-radius:12px}
@media(prefers-color-scheme:dark){body{background:#171d19;color:#e8eee8}li,.empty,input{background:#222c25;color:#e8eee8}a{color:#9bdfb5}p,small{color:#adbcad}}
"""


def _read_db(path: Path):
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def catalog() -> list[dict]:
    """List registered files only. Never enumerate a credential or browser directory."""
    out = []
    conn = _read_db(DATA_DIR / "moodle.db")
    if conn is not None:
        try:
            for row in conn.execute("SELECT f.path, f.title, f.section, f.size, c.code, c.folder "
                                    "FROM files f LEFT JOIN courses c ON c.id=f.course_id "
                                    "WHERE f.removed_at IS NULL ORDER BY c.folder, f.path"):
                out.append({"path": row["path"], "title": row["title"] or Path(row["path"]).name,
                            "course": row["code"] or row["folder"] or "Moodle", "section": row["section"] or "",
                            "bytes": row["size"]})
        except sqlite3.OperationalError:
            pass  # no successful sync / old database yet
        finally:
            conn.close()
    conn = _read_db(ED_DB)
    if conn is not None:
        try:
            for row in conn.execute("SELECT f.local_path, f.title, l.title AS lesson, c.code "
                                    "FROM lesson_files f JOIN lessons l ON l.id=f.lesson_id "
                                    "JOIN courses c ON c.id=l.course_id WHERE f.local_path IS NOT NULL"):
                rel = content_index.readable_path(Path(row["local_path"]))
                if rel:
                    out.append({"path": rel, "title": row["title"] or Path(rel).name,
                                "course": row["code"] or "Ed", "section": row["lesson"], "bytes": None})
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    return out


def page(rows: list[dict], query: str = "", course: str = "", offset: int = 0) -> str:
    esc = html.escape
    filtered = [row for row in rows if query.casefold() in (row["title"] + " " + row["path"] + " " + row["section"]).casefold()
                and course.casefold() in row["course"].casefold()]
    groups: dict[str, list[dict]] = {}
    for row in filtered[offset:offset + 200]:
        groups.setdefault(row["course"], []).append(row)
    sections = []
    for name, items in groups.items():
        links = []
        for row in items:
            href = "/download?" + urllib.parse.urlencode({"path": row["path"]})
            links.append(f'<li><a href="{esc(href, quote=True)}">{esc(row["title"])}</a>'
                         f'<small>{esc(row["section"])} · {esc(row["path"])}</small></li>')
        sections.append(f"<h2>{esc(name)}</h2><ul>{''.join(links)}</ul>")
    more = ""
    if offset + 200 < len(filtered):
        url = "/?" + urllib.parse.urlencode({"q": query, "course": course, "offset": offset + 200})
        more = f'<a href="{esc(url, quote=True)}">Next 200 / 下一页</a>'
    empty = '<p class="empty">No matching files. Run monash sync first. 尚无匹配课件，请先同步。</p>'
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Monash · Course library</title><style>{STYLE}</style><main><header><p>MONASH STUDY KIT · LOCAL LIBRARY</p>'
            '<h1>Your course files / 我的课件</h1><p>Already synced files, grouped by course and week. '
            '只读浏览已同步资料，点击文件即可下载。</p></header><form method="get" action="/">'
            f'<input name="course" aria-label="Course" placeholder="Course / 课号" value="{esc(course, quote=True)}">'
            f'<input name="q" aria-label="File or week" placeholder="File or week / 文件名或周次" value="{esc(query, quote=True)}">'
            f'<button>Find / 查找</button></form><p>{len(filtered)} files / 个文件</p>'
            f'{"".join(sections) or empty}{more}</main></html>')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # paths and filenames may be personal

    def _send(self, status, body=b"", content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        self._send(405, b"Read only")

    def do_GET(self):
        port = self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return self._send(403, b"Invalid host")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self._send(403, b"Cross-site access refused")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            return self._send(403, b"Invalid origin")
        url = urllib.parse.urlsplit(self.path)
        args = urllib.parse.parse_qs(url.query)
        rows = catalog()
        if url.path == "/":
            try:
                offset = max(0, int(args.get("offset", ["0"])[0]))
            except ValueError:
                return self._send(400, b"Invalid offset")
            return self._send(200, page(rows, args.get("q", [""])[0], args.get("course", [""])[0], offset).encode())
        if url.path == "/download":
            rel = args.get("path", [""])[0]
            if rel not in {row["path"] for row in rows}:
                return self._send(404, b"File not found")
            try:
                path = content_index.resolve_readable(rel)
                file = path.open("rb")
            except (ValueError, OSError):
                return self._send(404, b"File not found")
            with file:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(path.name))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    shutil.copyfileobj(file, self.wfile)
            return
        self._send(404, b"Not found")


def make_server(port: int = 8766) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def command(args) -> int:
    server = make_server(args.port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Course library: {url} (Ctrl-C to stop)")
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
