"""Monash Moodle 的最小客户端：会话 cookie、AJAX 接口、页面解析、文件下载。

登录靠浏览器里拿到的 cookie（Okta SAML 没法在这里走，见 browser_login.py）。Monash 关掉了
移动端 Web Service（tool_mobile_get_public_config 里 enablemobilewebservice=0），
所以 moodle-cli 那种"用移动端令牌无限续期"的办法在这里用不了，只能靠定时
core_session_touch 让会话一直不空闲。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from .paths import DATA_DIR, FILES_DIR, SECRETS_DIR, write_secret  # noqa: F401  FILES_DIR 给 syncer 等模块用

BASE_URL = os.environ.get("MOODLE_BASE_URL", "https://learning.monash.edu").rstrip("/")
HOST = urllib.parse.urlsplit(BASE_URL).hostname or ""
COOKIE_FILE = SECRETS_DIR / "moodle-cookie"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
# 请求之间歇一下：这是用个人账号访问学校系统，别像爬虫一样猛打
REQUEST_GAP = float(os.environ.get("MONASH_KIT_GAP", "0.4"))


class MoodleAuthError(RuntimeError):
    """会话失效或 cookie 不对。调用方应退出码 2，提示运行 `monash login`。"""


class MoodleError(RuntimeError):
    """Moodle 返回了意料之外的东西：HTTP 错误、跳转太多、AJAX 接口报错。命令行退出码 3。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- cookie

def parse_cookie_input(text: str) -> dict[str, str]:
    """从用户粘贴的东西里抠出 cookie。

    接受三种：DevTools 里 "Copy as cURL" 的整段命令、一行 `a=b; c=d` 的
    cookie 头、或者只有 MoodleSession 的值。
    """
    text = text.strip()
    if not text:
        return {}
    header = None
    if text.startswith("curl "):
        try:
            argv = shlex.split(text.replace("\\\n", " "))
        except ValueError:
            argv = text.split()
        for i, arg in enumerate(argv):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if arg in ("-b", "--cookie"):
                header = nxt
            elif arg in ("-H", "--header") and nxt.lower().startswith("cookie:"):
                header = nxt.split(":", 1)[1]
        if header is None:
            return {}
    elif "=" in text:
        header = text
    else:
        return {"MoodleSession": text}
    jar = {}
    for part in header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                jar[k.strip()] = v.strip()
    return jar


def load_cookies() -> dict[str, str]:
    if not COOKIE_FILE.exists():
        raise MoodleAuthError("还没登录 Moodle，先运行 `monash login`")
    try:
        jar = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        jar = parse_cookie_input(COOKIE_FILE.read_text(encoding="utf-8"))
    if not any(k.startswith("MoodleSession") for k in jar):
        raise MoodleAuthError(f"{COOKIE_FILE} 里没有 MoodleSession")
    return jar


def save_cookies(jar: dict[str, str]) -> None:
    write_secret(COOKIE_FILE, json.dumps(jar))


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


@dataclass
class Response:
    status: int
    url: str
    headers: object
    body: bytes = b""
    location: str | None = None

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class MoodleClient:
    def __init__(self, jar: dict[str, str] | None = None, persist: bool = True):
        self.jar = jar if jar is not None else load_cookies()
        self.persist = persist
        self._sesskey: str | None = None
        self.userid: int | None = None
        self._last = 0.0

    # 只有发往 Moodle 本站的请求才带 cookie。下载会被 302 到 CloudFront，
    # 自己一步步跟跳转，保证 MoodleSession 不会被带到别的域名去。
    def _request(self, url: str, *, data: bytes | None = None, headers: dict | None = None,
                 stream_to: Path | None = None) -> Response:
        gap = REQUEST_GAP - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("User-Agent", UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if urllib.parse.urlsplit(url).hostname == HOST:
            req.add_unredirected_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.jar.items()))
        try:
            resp = _opener.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            resp = e
        status = resp.status if hasattr(resp, "status") else resp.code
        if urllib.parse.urlsplit(url).hostname == HOST:
            self._absorb_cookies(resp.headers)
        location = resp.headers.get("Location")
        out = Response(status, url, resp.headers,
                       location=urllib.parse.urljoin(url, location) if location else None)
        if stream_to is not None and status == 200:
            tmp = stream_to.with_name(stream_to.name + ".part")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(tmp, "wb") as f:
                    while chunk := resp.read(1 << 16):
                        f.write(chunk)
            except BaseException:
                tmp.unlink(missing_ok=True)      # 下到一半断了，别在课件目录里留半个文件
                resp.close()
                raise
            os.replace(tmp, stream_to)
        else:
            out.body = resp.read()
        resp.close()
        return out

    def _absorb_cookies(self, headers) -> None:
        changed = False
        for raw in headers.get_all("Set-Cookie") or []:
            name, _, rest = raw.partition("=")
            value = rest.split(";", 1)[0]
            name = name.strip()
            if not name or value in ("deleted", ""):
                continue
            if self.jar.get(name) != value:
                self.jar[name] = value
                changed = True
        if changed and self.persist:
            save_cookies(self.jar)

    def get(self, url: str, max_hops: int = 8) -> Response:
        """GET 本站页面，只在本站内跟跳转；被踢去 Okta 就当会话失效。"""
        url = self.abs(url)
        for _ in range(max_hops):
            r = self._request(url)
            if not _is_redirect(r):
                return r
            _check_login_url(r.location)
            url = r.location
        raise MoodleError(f"跳转太多：{url}")

    def post(self, url: str, fields: list[tuple[str, str]] | None = None, *, body: bytes | None = None,
             content_type: str | None = None, max_hops: int = 8) -> Response:
        """POST 一个表单（或 multipart），然后像浏览器一样跟着 303 用 GET 走到最终页面。

        返回的 Response.url 是最终落地的地址，Moodle 的表单靠"落在哪一页"
        判断成功与否（比如生成日历链接后停在导出页）。
        """
        url = self.abs(url)
        if body is None:
            body = urllib.parse.urlencode(fields or []).encode()
            content_type = "application/x-www-form-urlencoded"
        r = self._request(url, data=body, headers={"Content-Type": content_type or "application/octet-stream"})
        for _ in range(max_hops):
            if not _is_redirect(r):
                return r
            _check_login_url(r.location)
            r = self._request(r.location)
        raise MoodleError(f"跳转太多：{url}")

    def html(self, url: str) -> str:
        r = self.get(url)
        if r.status != 200:
            raise MoodleError(f"GET {url} -> {r.status}")
        return r.text()

    @staticmethod
    def abs(url: str) -> str:
        return url if url.startswith("http") else BASE_URL + url


    # ---------------------------------------------------------- AJAX

    def sesskey(self) -> str:
        if self._sesskey is None:
            page = self.html("/my/courses.php")
            m = re.search(r'"sesskey":"([^"]+)"', page)
            if not m:
                raise MoodleAuthError("页面里没有 sesskey，大概是没登录上")
            self._sesskey = m.group(1)
        return self._sesskey

    def call(self, method: str, args: dict | None = None):
        url = f"{BASE_URL}/lib/ajax/service.php?sesskey={self.sesskey()}&info={method}"
        body = json.dumps([{"index": 0, "methodname": method, "args": args or {}}]).encode()
        r = self._request(url, data=body, headers={"Content-Type": "application/json"})
        if _is_redirect(r):
            _check_login_url(r.location)
        try:
            payload = json.loads(r.body)
        except json.JSONDecodeError:
            raise MoodleAuthError(f"{method} 返回的不是 JSON（{r.status}），会话可能已失效")
        first = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(first, dict) and first.get("error"):
            exc = first.get("exception") or {}
            code = exc.get("errorcode", "")
            if code in ("servicerequireslogin", "invalidsesskey", "requireloginerror"):
                raise MoodleAuthError(f"{method}: {code}")
            raise MoodleError(f"{method}: {exc.get('message') or first}")
        if isinstance(first, dict) and "exception" in first:
            raise MoodleError(f"{method}: {first.get('message')}")
        return first.get("data") if isinstance(first, dict) else first

    def time_remaining(self) -> int | None:
        data = self.call("core_session_time_remaining")
        if not isinstance(data, dict):
            return None
        self.userid = data.get("userid") or self.userid
        return data.get("timeremaining")

    def touch(self) -> int | None:
        self.call("core_session_touch")
        return self.time_remaining()

    def courses(self) -> list[dict]:
        data = self.call("core_course_get_enrolled_courses_by_timeline_classification",
                         {"classification": "all", "limit": 0, "offset": 0, "sort": "fullname"})
        return data["courses"]

    def course_state(self, course_id: int) -> dict:
        return json.loads(self.call("core_courseformat_get_state", {"courseid": course_id}))

    # ---------------------------------------------------------- 下载

    def resolve(self, url: str, max_hops: int = 8) -> tuple[str, str | None, Response]:
        """只在本站内跟跳转，不下载正文。

        返回 (最后一个本站 URL, 第一个站外地址或 None, 最后的响应)。本站 pluginfile
        地址里带着文件的 revision 和文件名，拿它当变更指纹；站外地址（CloudFront、
        Google Slides…）只记下来，不带 cookie 去碰。
        """
        url = self.abs(url)
        for _ in range(max_hops):
            r = self._request(url)
            if not _is_redirect(r):
                return url, None, r
            _check_login_url(r.location)
            if urllib.parse.urlsplit(r.location).hostname != HOST:
                return url, r.location, r
            url = r.location
        raise MoodleError(f"跳转太多：{url}")

    def download(self, url: str, dest: Path, max_hops: int = 8) -> Response:
        """下载到 dest。站外那一跳（CloudFront 签名链接）不带 cookie。"""
        url = self.abs(url)
        for _ in range(max_hops):
            r = self._request(url, stream_to=dest)
            if not _is_redirect(r):
                if r.status != 200:
                    raise MoodleError(f"下载失败 {url} -> {r.status}")
                return r
            _check_login_url(r.location)
            url = r.location
        raise MoodleError(f"跳转太多：{url}")


def _is_redirect(r: Response) -> bool:
    return bool(r.location) and r.status in (301, 302, 303, 307, 308)


def _check_login_url(url: str) -> None:
    """在跟进跳转之前检查：要是往 Okta / 登录页跳，就是会话没了。

    必须在请求之前查，不然下载时会把 Okta 登录页当成课件写进文件里。
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host.endswith("okta.com") or (host == HOST and parts.path.startswith("/login/")) \
            or (host == HOST and "/auth/saml" in parts.path):
        raise MoodleAuthError("Moodle 登录已过期（被跳到登录页），运行 `monash login` 重新登录")


def filename_from(resp: Response, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return urllib.parse.unquote(m.group(1))
    path = urllib.parse.urlsplit(resp.url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return name or fallback


# ---------------------------------------------------------------- HTML

class LinkCollector(HTMLParser):
    """按活动（li[data-id] / [data-for=cmitem]）归类收集链接。

    课程页里 label、CMS 块的内容只出现在 HTML 里，接口不给，所以得自己扫。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []  # (tag, activity id or None)
        self.by_activity: dict[str, list[tuple[str, str]]] = {}
        self.all: list[tuple[str, str]] = []
        self._a: tuple[str, list[str]] | None = None

    def _current(self) -> str | None:
        for _, act in reversed(self.stack):
            if act:
                return act
        return None

    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "area", "base", "col", "wbr"}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        act = None
        cls = a.get("class") or ""
        if tag == "li" and "activity" in cls.split() and a.get("data-id"):
            act = a["data-id"]
        if tag not in self.VOID:
            self.stack.append((tag, act))
        if tag == "a" and a.get("href"):
            self._a = (a["href"], [])

    def handle_endtag(self, tag):
        if tag == "a" and self._a:
            href, text = self._a
            item = (href, " ".join("".join(text).split()))
            self.all.append(item)
            act = self._current()
            if act:
                self.by_activity.setdefault(act, []).append(item)
            self._a = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._a:
            self._a[1].append(data)


def collect_links(html_text: str) -> LinkCollector:
    p = LinkCollector()
    p.feed(html_text)
    p.close()
    return p


DOC_EXT = re.compile(r"\.(pdf|pptx?|docx?|xlsx?|zip|tar|gz|tgz|7z|rar|ipynb|py|hs|ts|js|java|c|cpp|h|"
                     r"txt|md|csv|json|mzn|dzn|r|rmd|sql|mp4|mov|key|pages|numbers)$", re.I)


def is_attachment(url: str) -> bool:
    """本站 pluginfile 里真正的课件（排除主题图片、背景图之类）。"""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname != HOST or "/pluginfile.php/" not in parts.path:
        return False
    if re.search(r"/(theme_|msttools_imglib|user/icon|core_admin|block_)", parts.path):
        return False
    return bool(DOC_EXT.search(urllib.parse.unquote(parts.path)))


def db_connect(path: Path | None = None) -> sqlite3.Connection:
    # MCP 的后台同步和工具调用会同时开库：WAL 让读写互不阻塞，timeout 兜住偶尔的写锁
    path = path or DATA_DIR / "moodle.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS files (
        course_id   INTEGER NOT NULL,
        source      TEXT NOT NULL,      -- 本站最后一个 URL（含 revision），当指纹
        cmid        INTEGER,
        path        TEXT NOT NULL,      -- 相对 FILES_DIR
        size        INTEGER,
        section     TEXT,
        title       TEXT,
        synced_at   TEXT NOT NULL,
        removed_at  TEXT,               -- Moodle 上已经没有了：文件先留着，列表和搜索里不再出现
        PRIMARY KEY (course_id, source)
    );
    CREATE TABLE IF NOT EXISTS links (
        course_id INTEGER NOT NULL, folder TEXT NOT NULL, section TEXT,
        title TEXT, url TEXT NOT NULL, synced_at TEXT NOT NULL,
        PRIMARY KEY (course_id, folder, url)
    );
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY, code TEXT, folder TEXT, fullname TEXT,
        tracked INTEGER DEFAULT 0, synced_at TEXT
    );
    CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
    """)
    # 1.0.3 以前的库没有 removed_at，补上
    if "removed_at" not in {r["name"] for r in con.execute("PRAGMA table_info(files)")}:
        con.execute("ALTER TABLE files ADD COLUMN removed_at TEXT")
    return con


def removed_paths(con) -> set[str]:
    """Moodle 上已经下架的文件（相对 FILES_DIR）。"""
    return {r["path"] for r in con.execute("SELECT path FROM files WHERE removed_at IS NOT NULL")}


def set_state(con, k: str, v) -> None:
    con.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, json.dumps(v)))
    con.commit()


def get_state(con, k: str, default=None):
    row = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return json.loads(row["v"]) if row else default
