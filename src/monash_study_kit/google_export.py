"""课程里出现过的 Google 文件：只读 Drive API，失败时尝试专用浏览器的登录会话。"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import gmail
from .moodlelib import now_iso
from .paths import SECRETS_DIR, load_settings, write_secret

SCOPE = "https://www.googleapis.com/auth/drive.readonly"
TOKEN_FILE = SECRETS_DIR / "google-drive-token.json"
API = "https://www.googleapis.com/drive/v3/"
MAX_BYTES = 200 * 1024 * 1024
FORM_REASON = "Google 表单不能导出"
AUTH_REASON = "需要重新授权（monash google login）"
BROWSER_REASON = "浏览器里没登录 Google 或没有权限"
FORMATS = {
    "presentation": ("application/pdf", ".pdf", "pdf"),
    "document": ("application/pdf", ".pdf", "pdf"),
    "spreadsheets": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx", "xlsx"),
}
MIME_KIND = {"application/vnd.google-apps.presentation": "presentation",
             "application/vnd.google-apps.document": "document",
             "application/vnd.google-apps.spreadsheet": "spreadsheets"}


class GoogleError(Exception):
    def __init__(self, reason: str, fallback: bool = False):
        super().__init__(reason)
        self.fallback = fallback


def parse_link(url: str) -> tuple[str, str] | None:
    """只识别 Google 正式域名下的文件地址，不把普通网页当文件抓。"""
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("https", "http") or u.username or u.port:
            return None
    except ValueError:
        return None
    if u.hostname == "docs.google.com":
        if u.path.startswith("/forms/"):
            return "forms", ""
        m = re.match(r"/(presentation|document|spreadsheets)/d/([\w-]+)(?:/|$)", u.path)
        return (m[1], m[2]) if m else None
    if u.hostname == "drive.google.com":
        m = re.match(r"/file/d/([\w-]+)(?:/|$)", u.path)
        fid = m[1] if m else (urllib.parse.parse_qs(u.query).get("id") or [""])[0] if u.path == "/open" else ""
        return ("file", fid) if re.fullmatch(r"[\w-]+", fid) else None
    return None


def error_reason(status: int, reason: str = "", message: str = "") -> GoogleError:
    """把 Drive 的错误对应到索引里可操作的中文原因。"""
    if status == 401 or reason == "invalid_grant":
        return GoogleError(AUTH_REASON)
    if status == 404:
        return GoogleError("找不到或没共享给你")
    if reason in {"accessNotConfigured", "SERVICE_DISABLED"}:
        # OAuth 客户端所在的 Google Cloud 项目没打开 Drive API：先试浏览器，同时把原因写清楚
        return GoogleError("Google Cloud 项目还没启用 Drive API（项目管理员在控制台启用后重试）", True)
    if status == 403:
        if reason == "exportSizeLimitExceeded":
            return GoogleError("超出大小限制", True)
        if reason in {"domainPolicy", "cannotDownloadFile", "cannotExportFile"} or re.search(
                r"admin|domain policy|组织|管理员", message, re.I):
            return GoogleError("管理员限制", True)
        if reason in {"insufficientFilePermissions", "insufficientPermissions", "forbidden"}:
            return GoogleError("没有权限")
    return GoogleError(f"Google 下载失败（HTTP {status}）")


def _google():
    try:
        return gmail._google()
    except gmail.GmailError:
        raise GoogleError('请先安装 Google 授权依赖：uv tool install "monash-study-kit[gmail]"') from None


def login(client_file: Path) -> dict:
    """独立的 Drive 只读授权，不动 Gmail 的 scope 或 token。"""
    _, _, Flow = _google()
    config = json.loads(client_file.read_text(encoding="utf-8"))
    if "installed" not in config:
        raise GoogleError("请使用 Google 桌面应用的 OAuth 客户端 JSON")
    flow = Flow.from_client_config(config, scopes=[SCOPE])
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True, access_type="offline", prompt="consent",
        timeout_seconds=600, authorization_prompt_message="请在浏览器里授权 Google Drive 只读访问。",
        success_message="Google 已授权，可以关闭这个窗口。")
    scopes = credentials.granted_scopes or credentials.scopes or []
    if not credentials.refresh_token or SCOPE not in scopes:
        raise GoogleError("没有获得 Drive 只读权限或刷新令牌，请重新授权")
    write_secret(TOKEN_FILE, credentials.to_json())
    return {"已授权": True, "scope": scopes}


def status() -> dict:
    if not TOKEN_FILE.is_file():
        return {"已授权": False, "scope": []}
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        scopes = data.get("scopes", [])
        return {"已授权": bool(data.get("refresh_token")), "scope": scopes}
    except (ValueError, OSError):
        raise GoogleError(AUTH_REASON) from None


def revoke_token(token_file: Path) -> None:
    """只吊销指定文件里的授权，不删文件，供多文件全部吊销后再统一清理。"""
    if not token_file.is_file():
        return
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
        token = data.get("refresh_token") or data.get("token")
        if not token:
            return
        req = urllib.request.Request("https://oauth2.googleapis.com/revoke?" + urllib.parse.urlencode({"token": token}))
        with urllib.request.build_opener(gmail._NoRedirect()).open(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        # 400 invalid_token：授权早就失效了（过期，或者在 Google 账号页面撤销过），等于已经吊销
        body = e.read(4096).decode("utf-8", "replace")
        e.close()
        if e.code == 400 and "invalid_token" in body:
            return
        raise GoogleError("Google 授权吊销失败，本机令牌已保留，请重试") from None
    except Exception:
        raise GoogleError("Google 授权吊销失败，本机令牌已保留，请重试") from None


def logout() -> dict:
    """先吊销再删本机文件；吊销失败保留文件，方便重试。"""
    revoke_token(TOKEN_FILE)
    TOKEN_FILE.unlink(missing_ok=True)
    return {"已授权": False}


class Client:
    def __init__(self, credentials=None):
        self.credentials = credentials

    def _credentials(self):
        if self.credentials is None:
            if not TOKEN_FILE.is_file():
                raise GoogleError("还没授权 Google（monash google login）")
            _, Credentials, _ = _google()
            try:
                self.credentials = Credentials.from_authorized_user_file(str(TOKEN_FILE))
            except (ValueError, OSError):
                raise GoogleError(AUTH_REASON) from None
        if not self.credentials.valid:
            Request, _, _ = _google()
            try:
                self.credentials.refresh(Request())
            except Exception:
                raise GoogleError(AUTH_REASON) from None
            write_secret(TOKEN_FILE, self.credentials.to_json())
        return self.credentials

    def get(self, endpoint: str, **params) -> bytes:
        if not re.fullmatch(r"files/[\w-]+(?:/export)?", endpoint):
            raise ValueError("无效的 Google 文件接口")
        req = urllib.request.Request(API + endpoint + "?" + urllib.parse.urlencode(params))
        req.add_unredirected_header("Authorization", "Bearer " + self._credentials().token)
        try:
            with urllib.request.build_opener(gmail._NoRedirect()).open(req, timeout=60) as resp:
                raw = resp.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise GoogleError("超出大小限制", True)
                return raw
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read(65536)).get("error", {})
                errors = data.get("errors") or [{}]
                reason = errors[0].get("reason", "")
                message = data.get("message", "")
            except (ValueError, AttributeError, IndexError):
                reason = message = ""
            finally:
                e.close()
            raise error_reason(e.code, reason, message) from None
        except (OSError, ValueError):
            raise GoogleError("Google 下载失败，请检查网络后重试") from None


def browser_cookies() -> list[dict]:
    """从专用浏览器的 CDP 取 cookie，仅在内存使用，不记录内容。"""
    from .browser_login import Browser
    browser = Browser("about:blank", headless=True)
    try:
        return [c for c in browser.cdp.call("Storage.getCookies").get("cookies", [])
                if c.get("domain", "").lstrip(".") in {"google.com", "docs.google.com"}]
    finally:
        browser.close()


# 导出链接会跳到 *.googleusercontent.com 去真正下载（签名地址，cookie 按域名匹配，不会带过去）
GOOGLE_HOSTS = ("google.com", "googleusercontent.com")


def _google_host(host: str | None) -> bool:
    return bool(host) and any(host == h or host.endswith("." + h) for h in GOOGLE_HOSTS)


class _GoogleRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or not _google_host(u.hostname) or u.username or u.port:
            raise GoogleError("Google 导出跳转到了非 Google 域名，已停止")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def browser_export(kind: str, fid: str, fmt: str) -> bytes:
    """浏览器后备路径仍只发 GET，cookie 的域和路径由内存 CookieJar 匹配。"""
    if kind not in FORMATS or not re.fullmatch(r"[\w-]+", fid) or fmt not in {"pdf", "xlsx", "docx"}:
        raise GoogleError(BROWSER_REASON)
    try:
        jar = http.cookiejar.CookieJar()
        for c in browser_cookies():
            domain = c["domain"]
            jar.set_cookie(http.cookiejar.Cookie(
                0, c["name"], c["value"], None, False, domain, domain.startswith("."), domain.startswith("."),
                c.get("path", "/"), True, c.get("secure", True), None, True, None, None, {}))
        opener = urllib.request.build_opener(_GoogleRedirect(), urllib.request.HTTPCookieProcessor(jar))
        req = urllib.request.Request(f"https://docs.google.com/{kind}/d/{fid}/export?format={fmt}")
        with opener.open(req, timeout=60) as resp:
            raw = resp.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise GoogleError("超出大小限制")
            if "html" in resp.headers.get("Content-Type", "").lower() or raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                raise GoogleError(BROWSER_REASON)
            return raw
    except GoogleError:
        raise
    except Exception:
        raise GoogleError(BROWSER_REASON) from None


def download(url: str, client=None) -> tuple[str, bytes]:
    """返回安全文件名和文件内容；API 限制时先尝试浏览器后备路径。"""
    parsed = parse_link(url)
    if not parsed:
        raise GoogleError("不是支持的 Google 文件链接")
    kind, fid = parsed
    if kind == "forms":
        raise GoogleError(FORM_REASON)
    client = client or Client()
    name = "Google-" + fid
    try:
        meta = json.loads(client.get(f"files/{fid}", fields="name,mimeType,size", supportsAllDrives="true"))
        name = meta.get("name") or name
        kind = MIME_KIND.get(meta.get("mimeType"), kind if kind != "file" else "file")
        if kind in FORMATS:
            mime, ext, fmt = _format(kind)
            raw = client.get(f"files/{fid}/export", mimeType=mime)
        else:
            ext = ""
            if int(meta.get("size") or 0) > MAX_BYTES:
                raise GoogleError("超出大小限制", True)
            raw = client.get(f"files/{fid}", alt="media", supportsAllDrives="true")
    except GoogleError as e:
        if not e.fallback or kind not in FORMATS:
            raise
        _, ext, fmt = _format(kind)
        try:
            raw = browser_export(kind, fid, fmt)
        except GoogleError as fallback:
            raise GoogleError(f"{e}；后备导出失败：{fallback}") from None
    from .syncer import safe_name
    name = safe_name(name)
    if ext and not name.lower().endswith(ext):
        name += ext
    return name, raw


def _format(kind: str) -> tuple[str, str, str]:
    if kind == "document" and load_settings().get("google_docs_format") == "docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx", "docx"
    return FORMATS[kind]


def exported(con, cid: int, url: str) -> bool:
    """链接清单保留原始记录，只有对应本地文件还在时才隐藏链接。"""
    from . import lessons, syncer
    row = con.execute("SELECT path FROM files WHERE course_id=? AND source=? AND removed_at IS NULL", (cid, url)).fetchone()
    if not row:
        return False
    return (lessons.FILES_DIR / row[0][3:]).is_file() if row[0].startswith("ed:") else (syncer.FILES_DIR / row[0]).is_file()


def sync_course(con, course: dict, *, ed_conn=None, refresh=False, client=None, log=print) -> None:
    """每门 Moodle 课同步完成后，处理这门课 Moodle 和 Ed Lessons 的 Google 链接。"""
    from . import lessons, syncer
    cid = course["id"]
    root = syncer.course_folder(course)
    code = syncer.course_code(course)
    targets = [(dict(r), None) for r in con.execute("SELECT folder, title, url FROM links WHERE course_id=?", (cid,))]
    if ed_conn is not None:
        lessons.ensure_lessons_schema(ed_conn)
        for r in ed_conn.execute("SELECT f.*, c.code FROM lesson_files f JOIN courses c ON c.id=f.course_id WHERE f.type='webpage'"):
            if code and code.upper() in re.split(r"[/\s]+", (r["code"] or "").upper()):
                targets.append((dict(r), ed_conn))
    for r, ec in targets:
        url = r["url"]
        if not parse_link(url):
            continue
        if ec is None:
            directory = syncer.FILES_DIR / root / r["folder"]
        else:
            lesson = ec.execute('SELECT id, module_id, idx AS "index", title FROM lessons WHERE id=?', (r["lesson_id"],)).fetchone()
            modules = [dict(m) for m in ec.execute("SELECT id,name FROM modules WHERE course_id=?", (r["course_id"],))]
            all_lessons = [dict(l) for l in ec.execute('SELECT id,module_id,idx AS "index",title FROM lessons WHERE course_id=? ORDER BY idx IS NULL,idx,id', (r["course_id"],))]
            mdirs = lessons.module_folders(modules, all_lessons)
            pos = next(i for i,l in enumerate(all_lessons) if l["id"] == lesson["id"])
            directory = lessons.FILES_DIR / lessons.course_folder(r["code"], r["course_id"]) / mdirs.get(lesson["module_id"], mdirs[None]) / lessons.lesson_folder(dict(lesson), pos)
        try:
            old = con.execute("SELECT path FROM files WHERE course_id=? AND source=?", (cid, url)).fetchone()
            old_path = ((lessons.FILES_DIR / old[0][3:]) if old[0].startswith("ed:") else syncer.FILES_DIR / old[0]) if old else None
            if not refresh and old_path and old_path.is_file():
                name, raw = old_path.name, None
            else:
                name, raw = download(url, client)
            directory.mkdir(parents=True, exist_ok=True)
            dest = directory / name
            # 同名但不同来源的文件保留两份，也不覆盖课程原有附件。
            if dest.exists() and (old_path is None or dest != old_path):
                import hashlib
                dest = directory / (Path(name).stem + "-" + hashlib.sha256(url.encode()).hexdigest()[:8] + Path(name).suffix)
            if raw is not None or (old_path and dest != old_path):
                tmp = dest.with_name(dest.name + ".part")
                try:
                    tmp.write_bytes(raw if raw is not None else old_path.read_bytes())
                    os.replace(tmp, dest)
                finally:
                    tmp.unlink(missing_ok=True)
            if ec is not None:
                rel = dest.relative_to(lessons.FILES_DIR).as_posix()
                ec.execute("UPDATE lesson_files SET local_path=? WHERE id=?", (rel, r["id"]))
                ec.commit()
                path = "ed:" + rel
            else:
                path = dest.relative_to(syncer.FILES_DIR).as_posix()
            # 同时出现在 Moodle 和 Ed 的链接，索引记录优先保留 Moodle 的位置。
            if ec is None or not old or old[0].startswith("ed:"):
                con.execute("INSERT INTO files (course_id,source,path,size,title,synced_at) VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(course_id,source) DO UPDATE SET path=excluded.path,size=excluded.size,synced_at=excluded.synced_at,removed_at=NULL",
                            (cid,url,path,dest.stat().st_size,r["title"],now_iso()))
            con.execute("DELETE FROM notes WHERE course_id=? AND kind='google_failed' AND url=?", (cid,url))
        except Exception as e:
            reason = str(e) if isinstance(e, GoogleError) else "Google 导出失败，请检查网络或本地文件权限后重试"
            con.execute("INSERT OR REPLACE INTO notes VALUES (?,?,?,?,?,?,?)",
                        (cid,"google_failed",r.get("folder") or "Ed Lessons",r["title"],url,reason,now_iso()))
            log(f"  Google：{reason}")
        con.commit()
