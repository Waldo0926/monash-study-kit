"""课程笔记网页：Moodle 和 Ed Lessons 里链接到的讲义网站，抓下来转成文字，进全文索引。

很多课的讲义不在 Moodle / Ed 里，而在老师的公开网站上（FIT2102 → tgdwyer.github.io，
FIT2109 → yqtian-se.github.io）。只抓被课程直接链接到的那几页，不顺着页面里的链接往下爬。

哪些站算“讲义站”：
  * github.io / gitlab.io / readthedocs.io / netlify.app / pages.dev 这类静态站；
  * 或者同一门课里被链接了很多次的站（CLASS_SITE_MIN 次以上）；
  * 学校官网、Moodle / Ed 自己、视频、会议、网盘、Google 文档之类一律不抓。

存在 FILES_DIR/<课程文件夹>/Course notes (web)/<标题>.md，开头写来源网址。放在课件目录里，
list_files / read_file / search_content 就都能直接用。每页最多每 REFRESH_DAYS 天重抓一次，
带 ETag / Last-Modified 条件请求；请求之间歇 GAP 秒；robots.txt 不让抓的页面跳过。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import htmldom
from .edlib import DB_PATH as ED_DB
from .moodlelib import FILES_DIR, now_iso
from .syncer import safe_name

UA = "monash-study-kit (personal study helper; fetches course notes linked from Moodle/Ed)"
ROBOTS_AGENT = "monash-study-kit"     # robots.txt 里按这个名字匹配 User-agent
ROBOTS_TTL = 24 * 3600
STATIC_HOSTS = ("github.io", "gitlab.io", "readthedocs.io", "netlify.app", "pages.dev", "vercel.app")
BLOCKED_HOSTS = ("monash.edu", "monash.edu.my", "edstem.org", "zoom.us", "youtube.com", "youtu.be",
                 "google.com", "googleusercontent.com", "sharepoint.com", "microsoft.com", "office.com",
                 "okta.com", "instructure.com", "panopto.com", "vimeo.com", "github.com")
SKIP_EXT = re.compile(r"\.(zip|tar|gz|tgz|7z|rar|mp4|mov|mp3|png|jpe?g|gif|svg|ipynb|pptx?|docx?|xlsx?)$", re.I)
CLASS_SITE_MIN = 5        # 同一门课链到同一个站这么多次，就当它是讲义站
MAX_PAGES_PER_COURSE = 200
MAX_BYTES = 5 * 1024 * 1024
REFRESH_DAYS = 7
GAP = 1.0
FOLDER = "Course notes (web)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_pages (
    url TEXT PRIMARY KEY, course TEXT, title TEXT, path TEXT, etag TEXT, last_modified TEXT,
    status TEXT, fetched_at TEXT
);
"""


# ---------------------------------------------------------------- 挑页面

def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _host_matches(host: str, suffixes: tuple) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def normalize(url: str) -> str | None:
    p = urllib.parse.urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, ""))


def choose(links: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """[(课号, url, 标题)] → 要抓的那些。按课去重、过滤、每门课限量。"""
    per_course: dict[str, dict[str, str]] = {}
    for code, url, title in links:
        u = normalize(url)
        if code and u:
            per_course.setdefault(code, {}).setdefault(u, title or "")
    out = []
    for code, urls in per_course.items():
        counts = Counter(host_of(u) for u in urls)
        picked = []
        for u, title in urls.items():
            host = host_of(u)
            if _host_matches(host, BLOCKED_HOSTS) or SKIP_EXT.search(urllib.parse.urlsplit(u).path):
                continue
            if _host_matches(host, STATIC_HOSTS) or counts[host] >= CLASS_SITE_MIN:
                picked.append((code, u, title))
        out += picked[:MAX_PAGES_PER_COURSE]
    return out


def collect_links(moodle_con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """跟踪的课里所有外部链接：Moodle 的 links 表 + Ed Lessons 的网页 slide。"""
    rows = [(r["code"], r["url"], r["title"]) for r in moodle_con.execute(
        "SELECT c.code, l.url, l.title FROM links l JOIN courses c ON c.id = l.course_id WHERE c.tracked = 1")]
    if ED_DB.exists():
        try:
            con = sqlite3.connect(f"file:{ED_DB.as_posix()}?mode=ro", uri=True)
            for code, url, title in con.execute(
                    "SELECT c.code, f.url, f.title FROM lesson_files f JOIN courses c ON c.id = f.course_id "
                    "WHERE c.tracked = 1 AND f.type = 'webpage' AND f.url IS NOT NULL"):
                m = re.search(r"[A-Z]{3}\d{4}", code or "")
                rows.append((m.group(0) if m else None, url, title))
            con.close()
        except sqlite3.Error:
            pass
    return rows


# ---------------------------------------------------------------- HTML → 文字

DROP = {"script", "style", "nav", "header", "footer", "aside", "noscript", "svg", "form", "button",
        "iframe", "template", "select"}
BLOCK = {"p", "div", "section", "article", "main", "ul", "ol", "table", "tr", "blockquote", "figure",
         "details", "summary", "dl", "dt", "dd", "br", "hr"}


def html_to_text(html_text: str) -> tuple[str, str]:
    """返回 (标题, Markdown 风格的正文)。只保留阅读需要的：标题层级、段落、列表、代码块。"""
    doc = htmldom.parse(html_text)
    title_node = doc.find("title")
    title = title_node.text() if title_node else ""
    root = doc.find("main") or doc.find("article") or doc.find(role="main") or doc.find("body") or doc
    h1 = root.find("h1")
    if h1 and h1.text():
        title = h1.text()
    out: list[str] = []

    def walk(n):
        for ch in n.children:
            if isinstance(ch, str):
                out.append(re.sub(r"\s+", " ", ch))
                continue
            tag = ch.tag
            if tag in DROP or ch.get("hidden") is not None or ch.get("role") == "tooltip" \
                    or ch.classes & {"sr-only", "glossary-popup", "tooltip", "tooltip-text"}:
                continue       # 术语悬浮提示之类，混进正文会把句子打断
            if tag == "pre":
                code = "".join(t for t in _texts(ch)).rstrip()
                out.append(f"\n\n```\n{code}\n```\n\n")
            elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                out.append(f"\n\n{'#' * int(tag[1])} {ch.text()}\n\n")
            elif tag == "li":
                out.append("\n- ")
                walk(ch)
            elif tag == "code":
                out.append(f"`{ch.text()}`")
            elif tag in ("td", "th"):
                walk(ch)
                out.append(" | ")
            elif tag == "img":
                alt = ch.get("alt")
                if alt:
                    out.append(f"[图：{alt}]")
            else:
                if tag in BLOCK:
                    out.append("\n\n")
                walk(ch)
                if tag in BLOCK:
                    out.append("\n\n")

    walk(root)
    # 只整理代码块外面的空白：代码的缩进要原样保留（Haskell、Python 靠缩进）
    parts = re.split(r"(\n\n```\n.*?\n```\n\n)", "".join(out), flags=re.S)
    for i in range(0, len(parts), 2):
        t = re.sub(r"[ \t]+\n", "\n", parts[i])
        t = re.sub(r"\n[ \t]+", "\n", t)
        parts[i] = re.sub(r"\n{3,}", "\n\n", t)
    text = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()
    return " ".join(title.split()), text


def _texts(n):
    for ch in n.children:
        if isinstance(ch, str):
            yield ch
        else:
            if ch.tag == "br":
                yield "\n"
            yield from _texts(ch)


# ---------------------------------------------------------------- 抓取

_robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}


def load_robots(site: str) -> urllib.robotparser.RobotFileParser | None:
    """取 site 的 robots.txt。规则跟 urllib.robotparser.read() 一样：401/403 当全站禁止，
    别的 4xx 当没有限制；连不上返回 None（说不清，交给后面的正式请求去报错）。"""
    rp = urllib.robotparser.RobotFileParser(site + "/robots.txt")
    req = urllib.request.Request(site + "/robots.txt", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            rp.parse(r.read(512 * 1024).decode("utf-8", "replace").splitlines())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            rp.disallow_all = True
        else:
            rp.allow_all = True
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return rp


def robots_allowed(url: str, loader=load_robots) -> bool:
    """robots.txt 允不允许抓这一页。每个站一天最多取一次 robots.txt（MCP 会一直开着）。"""
    p = urllib.parse.urlsplit(url)
    site = f"{p.scheme}://{p.netloc}"
    at, rp = _robots.get(site, (0.0, None))
    if site not in _robots or time.time() - at > ROBOTS_TTL:
        rp = loader(site)
        _robots[site] = (time.time(), rp)
    return rp is None or rp.can_fetch(ROBOTS_AGENT, url)


def fetch(url: str, etag: str | None = None, last_modified: str | None = None) -> dict:
    """{"status": "ok"/"unchanged"/"skipped"/"error", ...}。不带任何登录信息。"""
    if not robots_allowed(url):
        return {"status": "skipped", "reason": "robots.txt 不允许"}
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    if etag:
        req.add_header("If-None-Match", etag)
    if last_modified:
        req.add_header("If-Modified-Since", last_modified)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in ("text/html", "application/xhtml+xml"):
                return {"status": "skipped", "reason": ctype or "未知类型"}
            body = r.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                return {"status": "skipped", "reason": "太大"}
            charset = r.headers.get_content_charset() or "utf-8"
            return {"status": "ok", "html": body.decode(charset, "replace"), "final_url": r.geturl(),
                    "etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified")}
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return {"status": "unchanged"}
        return {"status": "error", "reason": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"status": "error", "reason": str(getattr(e, "reason", e))}


def _course_folder(moodle_con, code: str) -> tuple[int | None, str]:
    row = moodle_con.execute("SELECT id, folder FROM courses WHERE code=? ORDER BY tracked DESC, id DESC LIMIT 1",
                             (code,)).fetchone()
    return (row["id"], row["folder"]) if row else (None, f"{code} (Ed)")


def _dest(folder: str, title: str, url: str, taken: set[str]) -> str:
    base = safe_name(title or urllib.parse.urlsplit(url).path.strip("/").replace("/", " ") or host_of(url), 70)
    rel = f"{folder}/{FOLDER}/{base}.md"
    if rel in taken:
        rel = f"{folder}/{FOLDER}/{base} ({hashlib.sha1(url.encode()).hexdigest()[:6]}).md"
    return rel


def sync(moodle_con: sqlite3.Connection, log=print, now: datetime | None = None, fetcher=fetch) -> dict:
    """抓该抓的讲义页。返回 {"fetched", "unchanged", "skipped", "errors"}。"""
    moodle_con.executescript(SCHEMA)
    now = now or datetime.now(timezone.utc)
    stale = (now - timedelta(days=REFRESH_DAYS)).isoformat(timespec="seconds")
    retry = (now - timedelta(days=1)).isoformat(timespec="seconds")     # 抓失败的第二天再试
    known = {r["url"]: dict(r) for r in moodle_con.execute("SELECT * FROM web_pages")}
    taken = {r["path"] for r in known.values() if r["path"]}
    stats = {"fetched": 0, "unchanged": 0, "skipped": 0, "errors": 0}
    last = 0.0
    for code, url, link_title in choose(collect_links(moodle_con)):
        prev = known.get(url)
        if prev and (prev["fetched_at"] or "") > (retry if (prev["status"] or "").startswith("error") else stale):
            continue
        wait = GAP - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        res = fetcher(url, (prev or {}).get("etag"), (prev or {}).get("last_modified"))
        course_id, folder = _course_folder(moodle_con, code)
        path = (prev or {}).get("path")
        if res["status"] == "ok":
            title, text = html_to_text(res["html"])
            if len(text) < 200:
                res = {"status": "skipped", "reason": "没有正文"}
            else:
                path = path or _dest(folder, title or link_title, url, taken)
                taken.add(path)
                dest = FILES_DIR / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f"# {title or link_title}\n\n来源：{url}\n\n{text}\n", encoding="utf-8")
                if course_id is not None:
                    moodle_con.execute(
                        "INSERT INTO files (course_id, source, cmid, path, size, section, title, synced_at)"
                        " VALUES (?,?,NULL,?,?,?,?,?) ON CONFLICT(course_id, source) DO UPDATE SET"
                        " path=excluded.path, size=excluded.size, synced_at=excluded.synced_at",
                        (course_id, f"web:{url}", path, dest.stat().st_size, FOLDER, title or link_title, now_iso()))
                log(f"  ↓ 讲义网页 {path}")
                link_title = title or link_title
        key = {"ok": "fetched", "unchanged": "unchanged", "skipped": "skipped"}.get(res["status"], "errors")
        stats[key] += 1
        if key == "errors":
            log(f"  ! {url}: {res.get('reason')}")
        moodle_con.execute(
            "INSERT OR REPLACE INTO web_pages VALUES (?,?,?,?,?,?,?,?)",
            (url, code, link_title, path if res["status"] in ("ok", "unchanged") else (prev or {}).get("path"),
             res.get("etag") or (prev or {}).get("etag"),
             res.get("last_modified") or (prev or {}).get("last_modified"),
             res["status"] if res["status"] != "error" else f"error: {res.get('reason')}",
             now.isoformat(timespec="seconds")))
        moodle_con.commit()
    return stats
