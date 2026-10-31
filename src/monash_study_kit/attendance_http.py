"""不开浏览器读考勤门户：用登录时保存的 cookie 直接请求页面，再用 Python 解析。

门户的 Units.aspx 用 #daySel 下拉框切换日期，每一天是一个 div#dayPanel_<日期>。如果这些
区块在服务器返回的 HTML 里就都有（只是隐藏），一次请求就能读到两周的班次，比开浏览器
一天一天切快得多。这一点要在真实门户上确认，所以：

  * 默认不用这条路（设置 attendance_fast_read），先用 `monash attendance probe` 对照；
  * 打开以后，只要页面结构对不上（没有 #daySel、某天的区块不在 HTML 里），就退回浏览器。

parse_day 是 attendance_assets/extract-rows.js 的 Python 版，只在当天的区块里找，
所以不需要浏览器的“是否可见”判断。两边的结果在测试里逐条对照。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie

from . import htmldom
from .attendance import ORIGIN, STATE_FILE, AttendanceAuthError, CACHE_FILE
from .paths import ensure_private_dir, write_secret

UNITS = ORIGIN + "/student/Units.aspx"
HOME_PAGE = ORIGIN + "/student/Default.aspx"
LOGIN_CLUE = re.compile(r"sign in to|登录您的帐户|session.*expired", re.I)


class FastPathUnavailable(RuntimeError):
    """页面结构和预期不一样，这次改用浏览器。"""


# ---------------------------------------------------------------- 请求

@contextmanager
def portal_lock():
    """和 attendance.browser() 用同一把锁，免得同时改登录状态文件。"""
    ensure_private_dir(CACHE_FILE.parent)
    lock = sqlite3.connect(CACHE_FILE.parent / "session-lock.db", timeout=2)
    lock.execute("CREATE TABLE IF NOT EXISTS lock (id INTEGER)")
    try:
        lock.execute("BEGIN IMMEDIATE")
        yield
    finally:
        lock.rollback()
        lock.close()


def _state() -> dict:
    if not STATE_FILE.is_file():
        raise AttendanceAuthError("Attendance is not logged in; run monash attendance login")
    return json.loads(STATE_FILE.read_text())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def fetch(url: str) -> tuple[str, list[str]]:
    """带 cookie GET 一个门户页面，返回 (HTML, Set-Cookie 列表)。跳去登录页就当登录过期。"""
    if not url.startswith(ORIGIN + "/student/"):
        raise ValueError("Only Attendance student pages are fetched")
    cookies = _state().get("cookies", [])
    request = urllib.request.Request(url, headers={
        "Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies),
        "User-Agent": "Mozilla/5.0 (monash-study-kit)",
    })
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read(8 * 1024 * 1024).decode(response.headers.get_content_charset() or "utf-8", "replace")
            set_cookies = response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        e.close()
        # 门户把过期的会话跳去 Monash 登录页；不跟过去，直接当登录过期
        if e.code in (301, 302, 303, 307, 308, 401, 403):
            raise AttendanceAuthError(f"Attendance session expired (HTTP {e.code}); run monash attendance login") from None
        raise RuntimeError(f"Attendance returned HTTP {e.code}") from None
    if LOGIN_CLUE.search(body[:20000]):
        raise AttendanceAuthError("Attendance session expired; reconnect with Monash sign-in")
    return body, set_cookies


def update_cookies(set_cookies: list[str]) -> int:
    """服务器续期时会发新的 cookie，写回登录状态文件，返回改了几个。"""
    if not set_cookies:
        return 0
    state = _state()
    cookies = {c["name"]: c for c in state.get("cookies", [])}
    host = urllib.parse.urlsplit(ORIGIN).hostname
    changed = 0
    for header in set_cookies:
        jar = SimpleCookie()
        try:
            jar.load(header)
        except CookieError:
            continue
        for name, morsel in jar.items():
            expires = -1
            if morsel["max-age"]:
                expires = time.time() + int(morsel["max-age"])
            elif morsel["expires"]:
                try:
                    expires = parsedate_to_datetime(morsel["expires"]).timestamp()
                except (TypeError, ValueError):
                    expires = -1
            if not morsel.value or (expires != -1 and expires < time.time()):
                changed += cookies.pop(name, None) is not None
                continue
            old = cookies.get(name, {"domain": host, "path": morsel["path"] or "/", "httpOnly": True,
                                     "secure": True, "sameSite": "Lax"})
            cookies[name] = {**old, "name": name, "value": morsel.value, "expires": expires}
            changed += 1
    state["cookies"] = list(cookies.values())
    write_secret(STATE_FILE, json.dumps(state))
    return changed


def keepalive() -> dict:
    """轻量续期：请求一次首页，有新 cookie 就存下来。不开浏览器。"""
    from . import attendance_auto
    try:
        with portal_lock():
            _, set_cookies = fetch(HOME_PAGE)
            renewed = update_cookies(set_cookies)
    except AttendanceAuthError:
        attendance_auto.record_failure()
        raise
    attendance_auto.record_ok()
    return {"authenticated": True, "renewed_cookies": renewed}


# ---------------------------------------------------------------- 解析（extract-rows.js 的 Python 版）

COURSE = re.compile(r"\b[A-Z]{3}\d{4}\b", re.I)
TYPE = re.compile(r"\b(workshop|tutorial|studio|applied(?:\s+class)?|practical|laboratory|lab|seminar)\b\s*0*(\d{1,2})?",
                  re.I)
TIME = re.compile(r"\b\d{1,2}:\d{2}\s*[ap]m\b", re.I)
NOT_DONE = re.compile(r"question|help|unknown|pending|incomplete")
DONE = re.compile(r"[✓✔☑]|(?:glyphicon|ui-icon|icon|fa|fas|far|fal|fab|bi)[-_ ]*(?:ok|check)(?:[-_ ]|\b)"
                  r"|\bcheck(?:ed|mark)?\b|\btick\b|\bcomplete(?:d)?\b|\bpresent\b|\bsuccess\b")
BLOCK = {"address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt", "fieldset",
         "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
         "main", "nav", "ol", "option", "p", "pre", "section", "select", "table", "tbody", "td", "tfoot", "th",
         "thead", "tr", "ul"}
CLUE_ATTRS = ("class", "data-icon", "title", "aria-label", "alt", "src")


def _compact(value) -> str:
    return " ".join(str(value or "").split())


def _done(value) -> bool:
    clue = str(value or "").lower()
    return not NOT_DONE.search(clue) and bool(DONE.search(clue))


def _hidden(node) -> bool:
    style = re.sub(r"\s+", "", (node.get("style") or "").lower())
    return (node.tag in ("script", "style", "template") or node.get("hidden") is not None
            or "display:none" in style or "visibility:hidden" in style
            or (node.tag == "input" and (node.get("type") or "").lower() == "hidden"))


def _visible(node, panel) -> bool:
    while node is not None and node is not panel:
        if _hidden(node):
            return False
        node = node.parent
    return True


def _text(node) -> str:
    parts = []

    def walk(n):
        for child in n.children:
            if isinstance(child, str):
                parts.append(child)
            elif not _hidden(child):
                block = child.tag in BLOCK
                if block:
                    parts.append(" ")
                walk(child)
                if block:
                    parts.append(" ")
    walk(node)
    return _compact("".join(parts))


def _outer_html(node) -> str:
    attrs = "".join(f' {k}="{v}"' for k, v in node.attrs.items())
    return f"<{node.tag}{attrs}>{node.inner_html()}</{node.tag}>"


def _synthetic_completed(href: str) -> bool:
    if not href:
        return False
    return urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("mah_completed") == ["1"]


def _matches(text: str) -> bool:
    return bool(COURSE.search(text) and TYPE.search(text) and TIME.search(text))


def _is_row(node) -> bool:
    cls = node.get("class") or ""
    return (node.tag in ("li", "tr") or node.get("role") == "listitem"
            or "ui-li" in cls or "row" in cls or "activity" in cls)


def _is_marker(node) -> bool:
    cls = node.get("class") or ""
    return (any(k in cls for k in ("glyphicon-ok", "ui-icon-check", "icon-ok", "check", "complete", "success"))
            or "check" in (node.get("data-icon") or "") or "complete" in (node.get("title") or "").lower()
            or any(k in (node.get("aria-label") or "").lower() for k in ("complete", "check"))
            or node.tag in ("svg", "i", "img", "span"))


def _entry_link(node):
    return next((n for n in node.iter() if n.tag == "a" and "Entry.aspx" in (n.get("href") or "")), None)


def parse_day(panel, key: str, page_url: str = UNITS) -> list[dict]:
    """一天的区块里的班次，字段和 extract-rows.js 返回的一样。"""
    sessions: list[dict] = []
    nodes = [panel, *panel.iter()]

    def href(link) -> str:
        return urllib.parse.urljoin(page_url, link.get("href") or "") if link is not None else ""

    def add(root, completed_hint=False, entry_href=""):
        if root is None or not _visible(root, panel):
            return
        text = _text(root)
        if not text or len(text) > 750:
            return
        course, kind, when = COURSE.search(text), TYPE.search(text), TIME.search(text)
        if not (course and kind and when):
            return
        number = int(kind.group(2)) if kind.group(2) else None
        session = re.sub(r"\s+", " ", kind.group(1)) + (f" {number:02d}" if number is not None else "")
        inner = [n for n in root.iter() if any(n.get(a) is not None for a in CLUE_ATTRS[:4])
                 or n.tag in ("img", "svg", "i", "span")][:80]
        clue = _compact(" ".join(" ".join(n.get(a) or "" for a in CLUE_ATTRS) for n in [root, *inner])
                        + " " + _outer_html(root)[:6000])
        synthetic = _synthetic_completed(entry_href)
        real = "" if synthetic else entry_href
        completed = synthetic or (not real and (completed_hint or _done(text + " " + clue)))
        row = {"course": course.group(0).upper(), "session": session, "attendanceLabel": text,
               "time": when.group(0), "entryUrl": "" if completed else real, "completed": completed,
               "sourceUrl": page_url}
        row_key = f"{key}|{row['course']}|{session.lower()}|{row['time'].lower()}"
        for i, item in enumerate(sessions):
            if item["_key"] == row_key:
                final = item["completed"] or completed
                sessions[i] = {**item, **row, "_key": row_key, "completed": final,
                               "entryUrl": "" if final else (row["entryUrl"] or item["entryUrl"] or "")}
                return
        sessions.append({"_key": row_key, **row})

    for link in [n for n in nodes if n.tag == "a" and "Entry.aspx" in (n.get("href") or "")]:
        root = link
        for _ in range(6):
            if root is panel or root.parent is None:
                break
            parent = root.parent
            text = _text(parent)
            if len(text) > 750:
                break
            root = parent
            if _matches(text):
                break
        add(root, _synthetic_completed(href(link)), href(link))

    for node in nodes:
        if not _is_row(node) or not _visible(node, panel):
            continue
        if not _matches(_text(node)):
            continue
        add(node, _synthetic_completed(href(_entry_link(node))), href(_entry_link(node)))

    for marker in nodes:
        if not _is_marker(marker):
            continue
        if not _done(" ".join(marker.get(a) or "" for a in CLUE_ATTRS)):
            continue
        root = marker
        for _ in range(8):
            if root is panel or root.parent is None:
                break
            root = root.parent
            text = _text(root)
            if len(text) > 750:
                break
            if _matches(text):
                add(root, True, href(_entry_link(root)))
                break

    return [{k: v for k, v in s.items() if k != "_key"} for s in sessions]


def day_options(document) -> list[str]:
    select = document.find("select", id="daySel")
    if select is None:
        raise FastPathUnavailable("The page has no #daySel day menu")
    return [o.get("value") for o in select.find_all("option") if o.get("value")]


def read_days(keys: list[str]) -> dict:
    """一次请求读多天。返回 {日期 key: 班次列表}；门户不提供的日期是 None。"""
    with portal_lock():
        html, set_cookies = fetch(UNITS)
        update_cookies(set_cookies)
    document = htmldom.parse(html)
    offered = set(day_options(document))
    out = {}
    for key in keys:
        if key not in offered:
            out[key] = None
            continue
        panel = document.find(id="dayPanel_" + key)
        if panel is None:
            # 下拉框里有这天，HTML 里却没有它的区块：多半是切换时才去服务器取的，这条路走不通
            raise FastPathUnavailable("Day panels are not in the page HTML")
        out[key] = parse_day(panel, key, UNITS)
    return out


# ---------------------------------------------------------------- 对照检查

def _signature(rows: list[dict]) -> list[tuple]:
    return sorted((r["course"], r["session"].lower(), r["time"].lower(), r["completed"], bool(r["entryUrl"]))
                  for r in rows)


def probe() -> dict:
    """在真实门户上对照两种读法。只输出数量和对不对得上，不输出课程、时间、链接等内容。"""
    from . import attendance, attendance_auto
    report: dict = {"portal": urllib.parse.urlsplit(ORIGIN).hostname, "session": attendance_auto.lifetime()}
    with portal_lock():
        html, set_cookies = fetch(UNITS)
    document = htmldom.parse(html)
    report["http"] = {"ok": True, "html_kb": len(html) // 1024, "set_cookie_headers": len(set_cookies)}
    try:
        keys = day_options(document)
    except FastPathUnavailable as e:
        report["http"]["day_menu"] = False
        report["verdict"] = f"不能用快速读取：{e}"
        return report
    panels = {k: document.find(id="dayPanel_" + k) for k in keys}
    fast = {k: parse_day(p, k) for k, p in panels.items() if p is not None}
    report["http"].update(day_menu=True, days_offered=len(keys), panels_in_html=len(fast),
                          rows_per_day=[len(fast[k]) for k in keys if k in fast])
    try:
        attendance._playwright()
    except RuntimeError:
        report["browser"] = "playwright 没装，只做了第一步"
        report["verdict"] = ("区块都在 HTML 里，可以试快速读取" if len(fast) == len(keys)
                             else "部分日期的区块不在 HTML 里，快速读取会退回浏览器")
        return report
    script = (attendance.ASSETS / "extract-rows.js").read_text()
    same, different, mismatch_kinds = 0, 0, set()
    dom_panels = 0
    with attendance.browser() as context:
        page = context.new_page()
        for i, key in enumerate(keys):
            ok = attendance._open_day(page, key) if i == 0 else attendance._select_day(page, key)
            if not ok:
                continue
            js_rows = page.evaluate(script, key).get("sessions", [])
            dom = htmldom.parse(page.content()).find(id="dayPanel_" + key)
            dom_panels += dom is not None
            py_rows = fast.get(key)
            if py_rows is None and dom is not None:
                py_rows = parse_day(dom, key)
            if py_rows is not None and _signature(py_rows) == _signature(js_rows):
                same += 1
            else:
                different += 1
                if py_rows is None:
                    mismatch_kinds.add("panel_missing")
                elif len(py_rows) != len(js_rows):
                    mismatch_kinds.add("row_count")
                else:
                    mismatch_kinds.add("fields")
    report["browser"] = {"days_compared": same + different, "same": same, "different": different,
                         "panels_after_render": dom_panels, "difference_kinds": sorted(mismatch_kinds)}
    if different == 0 and len(fast) == len(keys):
        report["verdict"] = "两种读法完全一致，可以打开：monash config attendance_fast_read true"
    else:
        report["verdict"] = "有不一致，先别打开快速读取，把这段结果发给维护者"
    return report
