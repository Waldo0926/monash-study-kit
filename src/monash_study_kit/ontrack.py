"""OnTrack / Doubtfire 纯 Python 客户端、会话续期、本地快照与任务查询。

设计参考并致谢 bunizao/ontrack-cli (MIT License, https://github.com/bunizao/ontrack-cli)，
以纯 Python 标准库 + pypdf 重新实现，复用 monash-study-kit 的专用浏览器配置（BROWSER_PROFILE）、
原子凭据存储（SECRETS_DIR）和 PDF 文本提取（textextract），无需 Node.js 或额外依赖。

安全边界：
  * 自定义 _NoRedirect 拦截所有 3xx，Username / Auth-Token / Cookie 绝不跨重定向泄露。
  * 会话与刷新 Cookie 仅保存在 SECRETS_DIR/ontrack-session.json（0600 权限，原子写入）。
  * 所有错误摘要都会脱敏 access_token / refresh_token。
  * 下载限制 256 MiB 并校验 PDF/ZIP 文件头与 Content-Range；上传限制 100 MiB 并防符号链接与 TOCTOU。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import local_state, paths, textextract
from .paths import DATA_DIR, SECRETS_DIR, ensure_private_dir, load_settings, page_timezone, save_settings, write_secret

SESSION_FILE = SECRETS_DIR / "ontrack-session.json"
CACHE_DIR = DATA_DIR / "ontrack"
CACHE_FILE = CACHE_DIR / "snapshot.json"
SHEETS_DIR = CACHE_DIR / "sheets"

MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 100 * 1024 * 1024
RENEW_MARGIN_SECONDS = 120
DEFAULT_TIMEOUT = 30.0
REQUEST_GAP = 0.15

STATUS_META: dict[str, dict[str, Any]] = {
    "ready_for_feedback": {"label": "Ready for Feedback", "final": False, "submitted": True},
    "not_started": {"label": "Not Started", "final": False, "submitted": False},
    "working_on_it": {"label": "Working On It", "final": False, "submitted": False},
    "need_help": {"label": "Need Help", "final": False, "submitted": False},
    "redo": {"label": "Redo", "final": False, "submitted": False},
    "feedback_exceeded": {"label": "Feedback Exceeded", "final": True, "submitted": True},
    "fix_and_resubmit": {"label": "Resubmit", "final": False, "submitted": False},
    "discuss": {"label": "Discuss", "final": False, "submitted": True},
    "demonstrate": {"label": "Demonstrate", "final": False, "submitted": True},
    "complete": {"label": "Complete", "final": True, "submitted": True},
    "fail": {"label": "Fail", "final": True, "submitted": True},
    "time_exceeded": {"label": "Time Exceeded", "final": True, "submitted": True},
    "assess_in_portfolio": {"label": "Assess in Portfolio", "final": True, "submitted": True},
    "attention_required": {"label": "Attention Required", "final": False, "submitted": True},
    "rediscuss": {"label": "Rediscuss", "final": False, "submitted": True},
}

ACTION_REQUIRED_STATUSES = frozenset({
    "redo",
    "fix_and_resubmit",
    "discuss",
    "demonstrate",
    "attention_required",
    "rediscuss",
})

WRITABLE_TASK_STATES = ("not_started", "working_on_it", "need_help")
SUBMISSION_TYPES = ("ready_for_feedback", "need_help", "assess_in_portfolio")

GRADE_FALLBACK: dict[int, dict[str, Any]] = {
    -1: {"id": "legacy-f", "value": -1, "name": "Fail", "abbreviation": "F"},
    0: {"id": "legacy-p", "value": 0, "name": "Pass", "abbreviation": "P"},
    1: {"id": "legacy-c", "value": 1, "name": "Credit", "abbreviation": "C"},
    2: {"id": "legacy-d", "value": 2, "name": "Distinction", "abbreviation": "D"},
    3: {"id": "legacy-hd", "value": 3, "name": "High Distinction", "abbreviation": "HD"},
}

UPLOAD_CONTENT_TYPES: dict[str, str] = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".zip": "application/zip",
}

SECRET_KEYS = frozenset({
    "authentication_token",
    "auth_token",
    "access_token",
    "refresh_token",
    "authenticationToken",
    "authToken",
    "accessToken",
    "refreshToken",
})


class OnTrackError(RuntimeError):
    """OnTrack 网络、接口契约或服务端错误。"""

    def __init__(self, message: str, status_code: int | None = None, hint: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.hint = hint


class OnTrackAuthError(OnTrackError):
    """未登录、会话失效或被 OnTrack 拒绝（401/419）。"""


class OnTrackNotFoundError(LookupError):
    """找不到对应的课程、项目或任务。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止 urllib 自动跟随重定向，避免 Username / Auth-Token / Cookie 泄露到站外。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect())


# ---------------------------------------------------------------- URL、时间、脱敏辅助

def normalize_base_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("请提供 OnTrack 站点地址（例如 https://ontrack.example.edu）")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.username or parts.password:
        raise ValueError("OnTrack 站点地址不能包含用户名或密码")
    if parts.query or parts.fragment:
        raise ValueError("OnTrack 站点地址不能包含查询参数或锚点")
    if not parts.hostname:
        raise ValueError(f"无效的 OnTrack 站点地址：{url}")
    is_local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and is_local):
        raise ValueError("OnTrack 站点地址必须使用 https://")
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def configured_base_url(explicit: str | None = None) -> str:
    for candidate in (
        explicit,
        os.environ.get("MONASH_KIT_ONTRACK_URL"),
        os.environ.get("ONTRACK_BASE_URL"),
        load_settings().get("ontrack_url"),
    ):
        if candidate and str(candidate).strip():
            return normalize_base_url(str(candidate))
    raise OnTrackAuthError(
        "尚未配置 OnTrack 站点地址。请运行 monash ontrack login --url https://<你的OnTrack域名>"
    )


def _redact(text: str, *secrets_to_hide: str | None) -> str:
    out = text
    for val in secrets_to_hide:
        if val:
            out = out.replace(val, "[redacted]")
    return out


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in SECRET_KEYS:
                continue
            safe = _sanitize_value(v)
            if isinstance(safe, dict) and not safe:
                continue
            out[k] = safe
        return out
    return value


def _parse_civil_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OnTrackError("OnTrack 日期字段必须是字符串")
    text = value.strip()
    if not text:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        raise OnTrackError(f"无法解析 OnTrack 日期：{value}")
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as e:
        raise OnTrackError(f"无效的 OnTrack 日期：{value}") from e


def _parse_instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OnTrackError("OnTrack 时间戳字段必须是字符串")
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise OnTrackError(f"无效的 OnTrack 时间戳：{value}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _local_today(now: datetime | None = None) -> date:
    dt = now or _now_utc()
    return dt.astimezone(page_timezone()).date()


def _is_future(iso_text: str | None, ref: datetime) -> bool:
    if not iso_text:
        return False
    try:
        dt = _parse_instant(iso_text)
    except OnTrackError:
        return False
    return dt is not None and dt > ref


def status_label(key: str) -> str:
    meta = STATUS_META.get(key)
    return meta["label"] if meta else key


def is_final_status(key: str) -> bool:
    meta = STATUS_META.get(key)
    return bool(meta["final"]) if meta else False


def grade_label(value: int | None, definitions: list[dict[str, Any]]) -> str:
    if value is None:
        return "-"
    for d in definitions:
        if d.get("value") == value:
            return f"{d['abbreviation']} ({d['name']})"
    if not definitions and value in GRADE_FALLBACK:
        d = GRADE_FALLBACK[value]
        return f"{d['abbreviation']} ({d['name']})"
    return str(value)


def safe_user_view(raw: Any, fallback_username: str | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "id": raw.get("id") if isinstance(raw.get("id"), int) else None,
        "username": raw.get("username") if isinstance(raw.get("username"), str) else fallback_username,
        "first_name": raw.get("first_name") or raw.get("firstName"),
        "last_name": raw.get("last_name") or raw.get("lastName"),
        "email": raw.get("email") if isinstance(raw.get("email"), str) else None,
        "nickname": raw.get("nickname") if isinstance(raw.get("nickname"), str) else None,
    }


def _domain_matches(hostname: str, cookie_domain: str) -> bool:
    norm = cookie_domain.lstrip(".").lower()
    host = hostname.lower()
    return bool(norm) and (host == norm or host.endswith("." + norm))


def _cookie_path_matches(req_path: str, cookie_path: str) -> bool:
    if not req_path.startswith(cookie_path):
        return False
    return len(req_path) == len(cookie_path) or cookie_path.endswith("/") or req_path[len(cookie_path)] == "/"


def _cookie_expired(expires: Any, now: datetime) -> bool:
    if expires in (None, "", 0, -1):
        return False
    if isinstance(expires, (int, float)):
        return float(expires) <= now.timestamp()
    try:
        dt = _parse_instant(str(expires))
        return dt is None or dt <= now
    except OnTrackError:
        return True


def filter_applicable_cookies(records: list[dict[str, Any]], base_url: str, now: datetime) -> list[dict[str, Any]]:
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname or ""
    is_https = parts.scheme == "https"
    out = []
    for c in records:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        path = c.get("path") if isinstance(c.get("path"), str) and c["path"].startswith("/") else "/"
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(value, str)
            or not value
            or not isinstance(domain, str)
            or not _domain_matches(host, domain)
            or (c.get("secure") is True and not is_https)
            or not _cookie_path_matches("/api/auth/access-token", path)
            or _cookie_expired(c.get("expires"), now)
            or re.search(r"[;\r\n]", name)
            or re.search(r"[;\r\n]", value)
        ):
            continue
        item: dict[str, Any] = {"name": name, "value": value, "domain": domain, "path": path}
        if "secure" in c and isinstance(c["secure"], bool):
            item["secure"] = c["secure"]
        if "expires" in c and isinstance(c["expires"], (int, float, str)):
            item["expires"] = c["expires"]
        out.append(item)
    return out


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _content_range(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    m = re.match(r"^bytes (\d+)-(\d+)/(\d+)$", value.strip())
    if not m:
        return None
    start, end, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if start < 0 or end < start or total <= end:
        return None
    return start, end, total


def _response_filename(disposition: str | None) -> str | None:
    if not disposition:
        return None
    filename = None
    ext_m = re.search(r"(?:^|;)\s*filename\*=UTF-8''([^;]+)", disposition, re.I)
    if ext_m:
        try:
            filename = urllib.parse.unquote(ext_m.group(1).strip())
        except Exception:  # noqa: BLE001
            filename = None
    if not filename:
        q_m = re.search(r'(?:^|;)\s*filename="([^"]*)"', disposition, re.I)
        if q_m:
            filename = q_m.group(1)
        else:
            p_m = re.search(r"(?:^|;)\s*filename=([^;]+)", disposition, re.I)
            if p_m:
                filename = p_m.group(1).strip()
    if not filename:
        return None
    safe = filename.replace("\\", "/").rsplit("/", 1)[-1]
    safe = re.sub(r"[\x00-\x1f\x7f]", "", safe).strip()
    return safe if safe and safe not in {".", ".."} else None


def is_valid_zip(data: bytes) -> bool:
    """校验 ZIP 结束目录与中央目录结构，避免把错误 HTML 当成压缩包保存。"""
    n = len(data)
    if n < 22:
        return False
    first = max(0, n - 22 - 0xFFFF)
    for offset in range(n - 22, first - 1, -1):
        if data[offset:offset + 4] != b"PK\x05\x06":
            continue
        disk, cd_disk, disk_entries, entries, cd_size, cd_offset, comment_len = struct.unpack_from(
            "<HHHHIIH", data, offset + 4
        )
        if offset + 22 + comment_len != n:
            continue
        if disk != 0 or cd_disk != 0 or disk_entries != entries or cd_offset + cd_size != offset:
            return False
        if entries == 0:
            return cd_size == 0
        cursor = cd_offset
        for _ in range(entries):
            if cursor + 46 > offset or data[cursor:cursor + 4] != b"PK\x01\x02":
                return False
            name_len, extra_len, cmt_len = struct.unpack_from("<HHH", data, cursor + 28)
            local_offset = struct.unpack_from("<I", data, cursor + 42)[0]
            if local_offset + 30 > cd_offset or data[local_offset:local_offset + 4] != b"PK\x03\x04":
                return False
            cursor += 46 + name_len + extra_len + cmt_len
        return cursor == offset
    return False


def is_valid_pdf(data: bytes) -> bool:
    return len(data) >= 5 and data[:5] == b"%PDF-"


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(". ")
    if not cleaned:
        return "file"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        cleaned = "_" + cleaned
    return cleaned[:120]


# ---------------------------------------------------------------- 底层 HTTP

def _read_bounded(resp, limit: int | None) -> bytes:
    if limit is None:
        return resp.read()
    cr = _content_range(resp.headers.get("Content-Range"))
    if cr and cr[2] > limit:
        raise OnTrackError(f"OnTrack 下载超过 {limit // (1024 * 1024)} MiB 上限")
    cl_raw = resp.headers.get("Content-Length")
    if cl_raw and cl_raw.strip().isdigit() and int(cl_raw.strip()) > limit:
        raise OnTrackError(f"OnTrack 下载超过 {limit // (1024 * 1024)} MiB 上限")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise OnTrackError(f"OnTrack 下载超过 {limit // (1024 * 1024)} MiB 上限")
        chunks.append(chunk)
    return b"".join(chunks)


def _upstream_error_detail(body: bytes) -> str | None:
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("error"), str):
        return None
    safe = re.sub(r"[\x00-\x1f\x7f]+", " ", parsed["error"])
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:500] if safe else None


def _http_send(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    limit: int | None = None,
    secret_values: tuple[str, ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        # 使用 add_unredirected_header 双重保证敏感头不跨重定向
        if k.lower() in {"auth-token", "username", "cookie", "authorization"}:
            req.add_unredirected_header(k, v)
        else:
            req.add_header(k, v)
    try:
        resp = _opener.open(req, timeout=timeout)
        try:
            data = _read_bounded(resp, limit)
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), hdrs, data
        finally:
            resp.close()
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read(MAX_ERROR_BYTES + 1)
            if len(err_body) > MAX_ERROR_BYTES:
                err_body = b""
        except OSError:
            err_body = b""
        hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
        try:
            e.close()
        except Exception:  # noqa: BLE001
            pass
        return int(e.code), hdrs, err_body
    except urllib.error.URLError as e:
        reason = _redact(str(e.reason), *secret_values)
        raise OnTrackError(f"连不上 OnTrack：{reason}") from e
    except TimeoutError as e:
        raise OnTrackError(f"OnTrack 请求超时（{int(timeout)} 秒）") from e
    except OSError as e:
        raise OnTrackError(f"OnTrack 网络错误：{_redact(str(e), *secret_values)}") from e


# ---------------------------------------------------------------- 认证：登录发现、Cookie 换 Token、会话缓存

def discover_auth_method(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    url = f"{normalize_base_url(base_url)}/api/auth/method"
    status, _, body = _http_send(url, headers={"Accept": "application/json"}, timeout=timeout)
    if status < 200 or status >= 300:
        raise OnTrackAuthError(f"无法获取 OnTrack 登录方式（HTTP {status}）", status_code=status)
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise OnTrackError("OnTrack /api/auth/method 返回了无效 JSON") from e
    if not isinstance(data, dict) or not isinstance(data.get("method"), str) or not data["method"].strip():
        raise OnTrackError("OnTrack /api/auth/method 响应缺少 method")
    redirect_to = data.get("redirect_to")
    if redirect_to is not None and not isinstance(redirect_to, str):
        raise OnTrackError("OnTrack /api/auth/method 的 redirect_to 格式无效")
    return {"method": data["method"].strip(), "redirect_to": redirect_to}


def discover_login_url(base_url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    base = normalize_base_url(base_url)
    info = discover_auth_method(base, timeout=timeout)
    method = info["method"]
    redirect_to = info.get("redirect_to")
    if method != "saml" and not (redirect_to and redirect_to.strip()):
        return f"{base}/sign_in"
    if not redirect_to or not redirect_to.strip():
        raise OnTrackError("OnTrack 未返回 SAML 登录地址")
    resolved = urllib.parse.urljoin(base + "/", redirect_to.strip())
    parts = urllib.parse.urlsplit(resolved)
    is_local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parts.scheme != "https" and not (parts.scheme == "http" and is_local)) or parts.username or parts.password:
        raise OnTrackError("OnTrack 返回的登录跳转地址无效")
    return resolved


def exchange_refresh_cookies(
    base_url: str,
    cookies: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """用 refresh_token (+ username) Cookie 换取短期 access_token。

    返回 None 表示服务端明确拒绝该 Cookie（401/403/419 或返回 null）；网络故障会抛 OnTrackError。
    """
    if not cookies:
        return None
    ref_now = now or _now_utc()
    base = normalize_base_url(base_url)
    cookie_val = _cookie_header(cookies)
    secrets_hide = tuple(c["value"] for c in cookies if c.get("value"))
    status, _, body = _http_send(
        f"{base}/api/auth/access-token",
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cookie": cookie_val,
        },
        body=b'{"delete_auth_token":false}',
        timeout=timeout,
        secret_values=secrets_hide,
    )
    if status in (401, 403, 419):
        return None
    if status < 200 or status >= 300:
        raise OnTrackError(f"换取 OnTrack 访问令牌失败（HTTP {status}）", status_code=status)
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError as e:
        raise OnTrackError("OnTrack 换取令牌返回了无效 JSON") from e
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise OnTrackAuthError("OnTrack 未返回有效的访问令牌")
    user = payload.get("user")
    token = payload.get("auth_token")
    expiry_raw = payload.get("auth_token_expiry")
    username = user.get("username") if isinstance(user, dict) else None
    if not isinstance(token, str) or not token or not isinstance(expiry_raw, str) or not isinstance(username, str) or not username:
        raise OnTrackAuthError("OnTrack 返回的会话信息不完整")
    expiry = _parse_instant(expiry_raw)
    if expiry is None or expiry <= ref_now:
        raise OnTrackAuthError("换取到的 OnTrack 访问令牌已过期")
    renewal = [c for c in cookies if c.get("name") in {"refresh_token", "username"}]
    return {
        "base_url": base,
        "username": username,
        "access_token": token,
        "auth_token_expiry": _iso_utc(expiry),
        "provenance": "browser",
        "user": safe_user_view(user, username),
        **({"refresh_cookies": renewal} if any(c.get("name") == "refresh_token" for c in renewal) else {}),
    }


def load_stored_session(base_url: str | None = None) -> dict[str, Any] | None:
    if not SESSION_FILE.is_file():
        return None
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if base_url and data.get("base_url") != normalize_base_url(base_url):
        return None
    if not isinstance(data.get("username"), str) or not data["username"]:
        return None
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        return None
    if not isinstance(data.get("base_url"), str) or not data["base_url"]:
        return None
    return data


def save_stored_session(session: dict[str, Any], *, keep_refresh: bool = True) -> dict[str, Any]:
    ensure_private_dir(SESSION_FILE.parent)
    lock_path = SESSION_FILE.with_suffix(".lock")
    with local_state.file_lock(lock_path):
        prev = load_stored_session(session["base_url"])
        to_save = dict(session)
        if keep_refresh and prev and prev.get("username") == session.get("username"):
            if "refresh_cookies" not in to_save and prev.get("refresh_cookies"):
                to_save["refresh_cookies"] = prev["refresh_cookies"]
        write_secret(SESSION_FILE, json.dumps(to_save, ensure_ascii=False, indent=2) + "\n")
    return to_save


def clear_session() -> dict[str, Any]:
    removed = False
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        removed = True
    return {"logged_out": True, "removed_session": removed}


def _env_credentials(base_url: str) -> dict[str, Any] | None:
    username = os.environ.get("MONASH_KIT_ONTRACK_USERNAME") or os.environ.get("ONTRACK_USERNAME")
    token = os.environ.get("MONASH_KIT_ONTRACK_TOKEN") or os.environ.get("ONTRACK_AUTH_TOKEN")
    if username and token:
        return {
            "base_url": normalize_base_url(base_url),
            "username": username.strip(),
            "access_token": token.strip(),
            "auth_token_expiry": None,
            "provenance": "environment",
            "user": None,
        }
    return None


def _is_fresh(expiry_iso: str | None, now: datetime) -> bool:
    if not expiry_iso:
        return False
    return _is_future(expiry_iso, now + timedelta(seconds=RENEW_MARGIN_SECONDS))


def login_browser(
    base_url: str,
    *,
    interactive: bool = True,
    timeout: float = 300.0,
    log=print,
) -> dict[str, Any]:
    """用 monash-study-kit 专用浏览器配置（BROWSER_PROFILE）登录 OnTrack，复用已有的 Okta 会话。"""
    from .browser_login import Browser, LoginFailed
    from .cdp import WebSocketClosed

    base = normalize_base_url(base_url)
    login_url = discover_login_url(base)
    try:
        browser = Browser(login_url, headless=not interactive)
    except LoginFailed as e:
        raise OnTrackAuthError(str(e)) from e

    if interactive:
        log(f"已打开 OnTrack 登录窗口（{base}）：完成登录并勾选 Remember me，成功后窗口会自动关闭。")

    started = time.monotonic()
    last_fingerprint = ""
    last_checked = 0.0
    saw_refresh = False
    try:
        while time.monotonic() - started < timeout:
            time.sleep(1.0)
            try:
                urls = browser.page_urls()
                if interactive and not urls:
                    raise OnTrackAuthError("OnTrack 登录窗口被关掉了，尚未完成登录（请重新运行 monash ontrack login）")
                raw_cookies = browser.cdp.call("Storage.getCookies").get("cookies", [])
            except (WebSocketClosed, OSError, RuntimeError) as e:
                raise OnTrackAuthError("OnTrack 登录窗口已关闭，尚未完成登录") from e

            now = _now_utc()
            applicable = filter_applicable_cookies(raw_cookies, base, now)
            relevant = [c for c in applicable if c["name"] in {"refresh_token", "username"}]
            if not any(c["name"] == "refresh_token" for c in relevant):
                continue
            saw_refresh = True
            fp = _cookie_header(relevant)
            if fp == last_fingerprint and (time.monotonic() - last_checked) < 5.0:
                continue
            last_fingerprint = fp
            last_checked = time.monotonic()
            session = exchange_refresh_cookies(base, relevant, now=now)
            if session is not None:
                return save_stored_session(session)

        if interactive and not saw_refresh:
            raise OnTrackAuthError(
                '等待登录超时，未收到刷新 Cookie。请在 OnTrack 登录页勾选 "Remember me" 后重试，'
                "或运行 monash ontrack login --paste。"
            )
        raise OnTrackAuthError("等待 OnTrack 登录超时" if interactive else "后台自动续期 OnTrack 会话超时")
    finally:
        browser.close()


def parse_pasted_credentials(raw: str, base_url: str, username: str | None = None) -> dict[str, Any]:
    """解析 --paste 粘贴的 JSON 响应、cURL 命令、Cookie 字符串或裸 refresh_token。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("未粘贴任何凭据内容")
    base = normalize_base_url(base_url)
    host = urllib.parse.urlsplit(base).hostname or ""

    # 1. JSON 响应（如 DevTools 中 /api/auth/access-token 的返回）
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError("粘贴的 JSON 格式无效") from e
        token = data.get("auth_token") or data.get("access_token")
        expiry = data.get("auth_token_expiry")
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        uname = user.get("username") or data.get("username") or username
        if not token or not uname:
            raise ValueError("JSON 中缺少 auth_token 或 username")
        expiry_dt = _parse_instant(expiry) if expiry else (_now_utc() + timedelta(minutes=10))
        if expiry_dt is None or expiry_dt <= _now_utc():
            raise OnTrackAuthError("粘贴的访问令牌已过期，请重新获取")
        return {
            "kind": "session",
            "session": {
                "base_url": base,
                "username": str(uname),
                "access_token": str(token),
                "auth_token_expiry": _iso_utc(expiry_dt),
                "provenance": "paste",
                "user": safe_user_view(user, str(uname)),
            },
        }

    # 2. cURL 命令或 Cookie 头
    cookie_str = None
    curl_cookie = re.search(r"(?:-H|--header)\s+['\"]cookie:\s*([^'\"]+)['\"]", text, re.I)
    if curl_cookie:
        cookie_str = curl_cookie.group(1)
    else:
        b_flag = re.search(r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]", text)
        if b_flag:
            cookie_str = b_flag.group(1)
        elif re.match(r"^cookie:\s*", text, re.I):
            cookie_str = re.sub(r"^cookie:\s*", "", text, flags=re.I).strip()
        elif "refresh_token=" in text:
            cookie_str = text

    cookies: list[dict[str, Any]] = []
    if cookie_str:
        for pair in cookie_str.split(";"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in {"refresh_token", "username"} and v:
                cookies.append({"name": k, "value": v, "domain": host, "path": "/"})
    else:
        # 3. 裸 refresh_token 值
        if re.search(r"[\s;]", text):
            raise ValueError("无法识别粘贴的格式：请粘贴 Cookie、cURL、access-token JSON 或单个 refresh_token")
        cookies.append({"name": "refresh_token", "value": text, "domain": host, "path": "/"})

    if not any(c["name"] == "refresh_token" for c in cookies):
        raise ValueError("粘贴的内容中没有找到 refresh_token")
    if username and not any(c["name"] == "username" for c in cookies):
        cookies.append({"name": "username", "value": username.strip(), "domain": host, "path": "/"})
    return {"kind": "cookies", "cookies": cookies}


def login_paste(base_url: str, raw: str, *, username: str | None = None) -> dict[str, Any]:
    base = normalize_base_url(base_url)
    parsed = parse_pasted_credentials(raw, base, username=username)
    if parsed["kind"] == "session":
        return save_stored_session(parsed["session"])
    session = exchange_refresh_cookies(base, parsed["cookies"])
    if session is None:
        raise OnTrackAuthError("OnTrack 拒绝了粘贴的 refresh_token（可能已过期或缺少 --username）")
    return save_stored_session(session)


def resolve_session(
    base_url: str | None = None,
    *,
    force_refresh: bool = False,
    rejected_token: str | None = None,
    allow_headless_browser: bool = True,
) -> dict[str, Any]:
    base = configured_base_url(base_url)
    env_sess = _env_credentials(base)
    if env_sess:
        return env_sess

    now = _now_utc()
    cached = load_stored_session(base)
    if rejected_token and cached and cached.get("access_token") == rejected_token:
        cached = {**cached, "access_token_invalidated": True}
        save_stored_session(cached)

    if (
        not force_refresh
        and cached
        and not cached.get("access_token_invalidated")
        and _is_fresh(cached.get("auth_token_expiry"), now)
    ):
        return cached

    # 1. 优先用 refresh_cookies 走纯 HTTP 换取新 token（快、不开浏览器）
    if cached and cached.get("refresh_cookies"):
        applicable = filter_applicable_cookies(cached["refresh_cookies"], base, now)
        renewed = exchange_refresh_cookies(base, applicable, now=now)
        if renewed is not None:
            if renewed["username"] != cached["username"]:
                raise OnTrackAuthError("刷新 Cookie 返回了另一个账号，请重新运行 monash ontrack login")
            return save_stored_session(renewed)
        # 服务端明确拒绝该 refresh cookie：清掉失效的 refresh_cookies
        stripped = {k: v for k, v in cached.items() if k != "refresh_cookies"}
        if not _is_future(cached.get("auth_token_expiry"), now) or cached.get("access_token_invalidated"):
            stripped["access_token_invalidated"] = True
        save_stored_session(stripped, keep_refresh=False)
        cached = stripped

    # 2. 如果专用浏览器目录存在且开启了 auto_relogin，静默无界面续期（复用 Moodle Okta 会话）
    settings = load_settings()
    if (
        allow_headless_browser
        and settings.get("auto_relogin", True)
        and (paths.BROWSER_PROFILE / "Default").exists()
    ):
        try:
            return login_browser(base, interactive=False, timeout=25.0, log=lambda *_: None)
        except OnTrackAuthError:
            pass

    # 3. 如果原 token 还没真正过期且只是提前续期失败，继续用完剩余时间
    if (
        not force_refresh
        and cached
        and not cached.get("access_token_invalidated")
        and _is_future(cached.get("auth_token_expiry"), _now_utc())
    ):
        return cached

    raise OnTrackAuthError("OnTrack 未登录或会话已过期。请运行 monash ontrack login 重新登录。")


def local_status() -> dict[str, Any]:
    settings = load_settings()
    url = os.environ.get("MONASH_KIT_ONTRACK_URL") or os.environ.get("ONTRACK_BASE_URL") or settings.get("ontrack_url") or ""
    stored = load_stored_session()
    now = _now_utc()
    refresh_expiry = None
    if stored and stored.get("refresh_cookies"):
        for c in stored["refresh_cookies"]:
            if c.get("name") == "refresh_token" and c.get("expires") is not None:
                exp = c["expires"]
                if isinstance(exp, (int, float)):
                    refresh_expiry = _iso_utc(datetime.fromtimestamp(float(exp), tz=timezone.utc))
                else:
                    refresh_expiry = str(exp)
    cache = load_cache()
    return {
        "enabled": bool(settings.get("ontrack_enabled")),
        "write_enabled": bool(settings.get("ontrack_write_enabled")),
        "base_url": url or (stored or {}).get("base_url") or None,
        "configured": bool(stored or (url and _env_credentials(url))),
        "username": (stored or {}).get("username"),
        "provenance": (stored or {}).get("provenance"),
        "access_token_valid": bool(stored and not stored.get("access_token_invalidated") and _is_future(stored.get("auth_token_expiry"), now)),
        "access_token_expiry": (stored or {}).get("auth_token_expiry"),
        "has_refresh_cookie": bool(stored and any(c.get("name") == "refresh_token" for c in stored.get("refresh_cookies") or [])),
        "refresh_cookie_expiry": refresh_expiry,
        "cached_units": len(cache.get("projects", [])) if cache else 0,
        "cached_tasks": sum(len(p.get("tasks", [])) for p in (cache or {}).get("projects", [])),
        "last_sync": (cache or {}).get("synced_at"),
    }


# ---------------------------------------------------------------- 领域解析与快照

def _req_int(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise OnTrackError(f"OnTrack 字段 {name} 必须是正整数")
    return v


def _opt_int(v: Any, name: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise OnTrackError(f"OnTrack 字段 {name} 必须是数字")
    return int(v)


def _req_str(v: Any, name: str) -> str:
    if not isinstance(v, str):
        raise OnTrackError(f"OnTrack 字段 {name} 必须是字符串")
    return v


def _opt_str(v: Any, name: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise OnTrackError(f"OnTrack 字段 {name} 必须是字符串")
    return v


def _opt_bool(v: Any, name: str) -> bool | None:
    if v is None:
        return None
    if not isinstance(v, bool):
        raise OnTrackError(f"OnTrack 字段 {name} 必须是布尔值")
    return v


def parse_grade_definitions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise OnTrackError("grade_definitions 必须是列表")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise OnTrackError("grade_definition 必须是对象")
        gid = _req_str(item.get("id"), "grade.id")
        val = _opt_int(item.get("value"), "grade.value")
        if val is None:
            raise OnTrackError("grade.value 缺失")
        name = item.get("name") or item.get("label") or (GRADE_FALLBACK.get(val) or {}).get("name")
        abbr = item.get("abbreviation")
        if not isinstance(name, str) or not isinstance(abbr, str):
            raise OnTrackError("grade.name 或 abbreviation 无效")
        out.append({"id": gid, "value": val, "name": name, "abbreviation": abbr})
    return out


def parse_unit_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnTrackError("unit 必须是对象")
    return {
        "id": _req_int(value.get("id"), "unit.id"),
        "code": _req_str(value.get("code"), "unit.code"),
        "name": _req_str(value.get("name"), "unit.name"),
        "my_role": _opt_str(value.get("my_role"), "unit.my_role"),
        "start_date": (d.isoformat() if (d := _parse_civil_date(value.get("start_date"))) else None),
        "end_date": (d.isoformat() if (d := _parse_civil_date(value.get("end_date"))) else None),
        "active": _opt_bool(value.get("active"), "unit.active"),
        "allow_flexible_dates": _opt_bool(value.get("allow_flexible_dates"), "unit.allow_flexible_dates"),
    }


def parse_upload_requirements(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise OnTrackError("upload_requirements 必须是列表")
    out = []
    for i, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise OnTrackError(f"upload_requirement #{i} 必须是对象")
        key = _req_str(item.get("key"), f"upload_requirement #{i}.key")
        if not re.fullmatch(r"file\d+", key):
            raise OnTrackError(f"upload_requirement #{i}.key 必须形如 file0")
        utype = _req_str(item.get("type"), f"upload_requirement #{i}.type")
        if utype not in {"code", "document", "image", "zip"}:
            raise OnTrackError(f"upload_requirement #{i}.type 不受支持：{utype}")
        out.append({
            "key": key,
            "name": _req_str(item.get("name"), f"upload_requirement #{i}.name"),
            "type": utype,
            "submission_history": bool(_opt_bool(item.get("submission_history"), "submission_history") or False),
        })
    return out


def parse_task_definition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnTrackError("task_definition 必须是对象")
    grade_due: dict[str, str] = {}
    grade_start: dict[str, str] = {}
    raw_gdd = value.get("grade_due_dates")
    if raw_gdd is not None:
        if not isinstance(raw_gdd, list):
            raise OnTrackError("grade_due_dates 必须是列表")
        for entry in raw_gdd:
            if not isinstance(entry, dict):
                raise OnTrackError("grade_due_date 必须是对象")
            tg = _opt_int(entry.get("target_grade"), "grade_due_date.target_grade")
            if tg is None:
                continue
            due = _parse_civil_date(entry.get("target_due_date"))
            start = _parse_civil_date(entry.get("start_date"))
            if due:
                grade_due[str(tg)] = due.isoformat()
            if start:
                grade_start[str(tg)] = start.isoformat()
    return {
        "id": _req_int(value.get("id"), "task_definition.id"),
        "abbreviation": _req_str(value.get("abbreviation"), "task_definition.abbreviation"),
        "name": _req_str(value.get("name"), "task_definition.name"),
        "description": _opt_str(value.get("description"), "task_definition.description"),
        "target_grade": _opt_int(value.get("target_grade"), "task_definition.target_grade"),
        "start_date": (d.isoformat() if (d := _parse_civil_date(value.get("start_date"))) else None),
        "target_date": (d.isoformat() if (d := _parse_civil_date(value.get("target_date"))) else None),
        "due_date": (d.isoformat() if (d := _parse_civil_date(value.get("due_date"))) else None),
        "is_graded": _opt_bool(value.get("is_graded"), "task_definition.is_graded"),
        "max_quality_pts": _opt_int(value.get("max_quality_pts"), "task_definition.max_quality_pts"),
        "has_task_sheet": _opt_bool(value.get("has_task_sheet"), "task_definition.has_task_sheet"),
        "has_task_resources": _opt_bool(value.get("has_task_resources"), "task_definition.has_task_resources"),
        "upload_requirements": parse_upload_requirements(value.get("upload_requirements")),
        "grade_due_dates": grade_due,
        "grade_start_dates": grade_start,
    }


def parse_task(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnTrackError("task 必须是对象")
    num_new = value.get("num_new_comments", 0)
    if num_new is None:
        num_new = 0
    if isinstance(num_new, bool) or not isinstance(num_new, int) or num_new < 0:
        raise OnTrackError("task.num_new_comments 必须是非负整数")
    return {
        "id": _req_int(value.get("id"), "task.id"),
        "task_definition_id": _req_int(value.get("task_definition_id"), "task.task_definition_id"),
        "status": _req_str(value.get("status"), "task.status"),
        "due_date": (d.isoformat() if (d := _parse_civil_date(value.get("due_date"))) else None),
        "target_due_date": (d.isoformat() if (d := _parse_civil_date(value.get("target_due_date"))) else None),
        "target_start_date": (d.isoformat() if (d := _parse_civil_date(value.get("target_start_date"))) else None),
        "submission_date": (d.isoformat() if (d := _parse_civil_date(value.get("submission_date"))) else None),
        "completion_date": (d.isoformat() if (d := _parse_civil_date(value.get("completion_date"))) else None),
        "moved_to_discuss_at": _iso_utc(_parse_instant(value.get("moved_to_discuss_at"))),
        "discuss_timeout_expiry_at": _iso_utc(_parse_instant(value.get("discuss_timeout_expiry_at"))),
        "extensions": _opt_int(value.get("extensions"), "task.extensions"),
        "times_assessed": _opt_int(value.get("times_assessed"), "task.times_assessed"),
        "grade": _opt_int(value.get("grade"), "task.grade"),
        "quality_pts": _opt_int(value.get("quality_pts"), "task.quality_pts"),
        "include_in_portfolio": _opt_bool(value.get("include_in_portfolio"), "task.include_in_portfolio"),
        "num_new_comments": num_new,
    }


def parse_projects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise OnTrackError("projects 必须是列表")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise OnTrackError("project summary 必须是对象")
        out.append({
            "id": _req_int(item.get("id"), "project.id"),
            "unit": parse_unit_summary(item.get("unit")),
            "target_grade": _opt_int(item.get("target_grade"), "project.target_grade"),
            "portfolio_available": _opt_bool(item.get("portfolio_available"), "project.portfolio_available"),
            "user_id": _opt_int(item.get("user_id"), "project.user_id"),
            "unit_id": _opt_int(item.get("unit_id"), "project.unit_id"),
        })
    return out


def parse_project(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnTrackError("project 必须是对象")
    unit = parse_unit_summary(value.get("unit"))
    raw_tasks = value.get("tasks")
    if not isinstance(raw_tasks, list):
        raise OnTrackError("project.tasks 必须是列表")
    return {
        "id": _req_int(value.get("id"), "project.id"),
        "unit": unit,
        "target_grade": _opt_int(value.get("target_grade"), "project.target_grade"),
        "submitted_grade": _opt_int(value.get("submitted_grade"), "project.submitted_grade"),
        "compile_portfolio": _opt_bool(value.get("compile_portfolio"), "project.compile_portfolio"),
        "portfolio_available": _opt_bool(value.get("portfolio_available"), "project.portfolio_available"),
        "uses_draft_learning_summary": _opt_bool(
            value.get("uses_draft_learning_summary"), "project.uses_draft_learning_summary"
        ),
        "flexible_dates": bool(unit.get("allow_flexible_dates") or False),
        "special_consideration_days": _opt_int(value.get("spec_con_days"), "project.spec_con_days") or 0,
        "tasks": [parse_task(t) for t in raw_tasks],
    }


def parse_unit(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnTrackError("unit 必须是对象")
    summary = parse_unit_summary(value)
    raw_defs = value.get("task_definitions")
    if not isinstance(raw_defs, list):
        raise OnTrackError("unit.task_definitions 必须是列表")
    return {
        **summary,
        "description": _opt_str(value.get("description"), "unit.description"),
        "grade_definitions": parse_grade_definitions(value.get("grade_definitions")),
        "task_definitions": [parse_task_definition(td) for td in raw_defs],
    }


def parse_task_comment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnTrackError("task comment 必须是对象")
    created_at = _parse_instant(value.get("created_at"))
    if created_at is None:
        raise OnTrackError("task comment 缺少 created_at")
    read_at = _parse_instant(value.get("recipient_read_time"))

    def _party(obj: Any, label: str) -> dict[str, Any]:
        if not isinstance(obj, dict):
            raise OnTrackError(f"{label} 必须是对象")
        return {
            "id": _req_int(obj.get("id"), f"{label}.id"),
            "first_name": _req_str(obj.get("first_name"), f"{label}.first_name"),
            "last_name": _req_str(obj.get("last_name"), f"{label}.last_name"),
            "email": _req_str(obj.get("email"), f"{label}.email"),
        }

    reply_to = value.get("reply_to_id")
    out = {
        "id": _req_int(value.get("id"), "comment.id"),
        "comment": _req_str(value.get("comment"), "comment.comment"),
        "has_attachment": bool(value.get("has_attachment")),
        "type": _req_str(value.get("type"), "comment.type"),
        "is_new": bool(value.get("is_new")),
        "reply_to_id": None if reply_to is None else _req_int(reply_to, "comment.reply_to_id"),
        "author": _party(value.get("author"), "comment.author"),
        "recipient": _party(value.get("recipient"), "comment.recipient"),
        "created_at": _iso_utc(created_at),
        "recipient_read_time": _iso_utc(read_at),
    }
    if "status" in value and value["status"] is not None:
        out["status"] = _req_str(value["status"], "comment.status")
    return out


def parse_roles(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise OnTrackError("roles 必须是列表")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise OnTrackError("role 必须是对象")
        out.append({
            "id": _req_int(item.get("id"), "role.id"),
            "role": _req_str(item.get("role"), "role.role"),
            "unit": parse_unit_summary(item.get("unit")),
            "user": safe_user_view(item.get("user")),
        })
    return out


def _effective_due(project: dict[str, Any], task: dict[str, Any], definition: dict[str, Any] | None) -> date | None:
    if project.get("flexible_dates"):
        if task.get("target_due_date"):
            return _parse_civil_date(task["target_due_date"])
        tg = project.get("target_grade")
        if definition and tg is not None and str(tg) in definition.get("grade_due_dates", {}):
            return _parse_civil_date(definition["grade_due_dates"][str(tg)])
    return _parse_civil_date(task.get("due_date")) or (
        _parse_civil_date(definition.get("target_date")) if definition else None
    )


def _effective_start(project: dict[str, Any], task: dict[str, Any], definition: dict[str, Any] | None) -> date | None:
    if project.get("flexible_dates"):
        if task.get("target_start_date"):
            return _parse_civil_date(task["target_start_date"])
        tg = project.get("target_grade")
        if definition and tg is not None and str(tg) in definition.get("grade_start_dates", {}):
            return _parse_civil_date(definition["grade_start_dates"][str(tg)])
    base = _parse_civil_date(definition.get("start_date")) if definition else None
    ext = task.get("extensions")
    if base and ext is not None and ext < 0:
        return base + timedelta(days=ext * 7)
    return base


def build_project_snapshot(
    project: dict[str, Any],
    unit: dict[str, Any],
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    ref_now = now or _now_utc()
    ref_today = today or _local_today(ref_now)
    proj = dict(project)
    if unit.get("allow_flexible_dates") is not None:
        proj["flexible_dates"] = bool(unit["allow_flexible_dates"])
    spec_days = int(proj.get("special_consideration_days") or 0)
    defs = {td["id"]: td for td in unit.get("task_definitions", [])}
    grade_defs = unit.get("grade_definitions", [])

    scheduled: list[tuple[date | None, dict[str, Any]]] = []
    for t in proj.get("tasks", []):
        td = defs.get(t["task_definition_id"])
        due = _effective_due(proj, t, td)
        start = _effective_start(proj, t, td)
        deadline = (due + timedelta(days=spec_days)) if due else None
        discuss_exp = _parse_instant(t.get("discuss_timeout_expiry_at"))
        abbr = td["abbreviation"] if td else f"TD-{t['task_definition_id']}"
        row = {
            "id": t["id"],
            "task_definition_id": t["task_definition_id"],
            "abbreviation": abbr,
            "name": td["name"] if td else "Unknown task",
            "status": t["status"],
            "status_label": status_label(t["status"]),
            "target_grade": td["target_grade"] if td else None,
            "target_grade_label": grade_label(td["target_grade"] if td else None, grade_defs),
            "start_date": start.isoformat() if start else None,
            "target_date": td["target_date"] if td else None,
            "target_start_date": t["target_start_date"],
            "target_due_date": t["target_due_date"],
            "due_date": due.isoformat() if due else None,
            "deadline": deadline.isoformat() if deadline else None,
            "submission_date": t["submission_date"],
            "completion_date": t["completion_date"],
            "moved_to_discuss_at": t["moved_to_discuss_at"],
            "discuss_timeout_expiry_at": t["discuss_timeout_expiry_at"],
            "extensions": t["extensions"],
            "grade": t["grade"],
            "grade_label": grade_label(t["grade"], grade_defs),
            "quality_pts": t["quality_pts"],
            "include_in_portfolio": t["include_in_portfolio"],
            "unread_comments": int(t.get("num_new_comments") or 0),
            "is_overdue": bool(deadline and deadline < ref_today and not is_final_status(t["status"])),
            "is_discuss_overdue": bool(discuss_exp and discuss_exp < ref_now),
        }
        scheduled.append((due, row))

    scheduled.sort(key=lambda item: (item[0] is None, item[0] or date.max, item[1]["abbreviation"], item[1]["id"]))
    return {
        "project": proj,
        "unit": unit,
        "tasks": [row for _, row in scheduled],
    }


# ---------------------------------------------------------------- 上传校验与编码

def prepare_uploads(file_paths: list[str], requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(file_paths) != len(requirements):
        raise ValueError(
            f"该任务需要按要求顺序提交 {len(requirements)} 个文件，但只收到 {len(file_paths)} 个。"
        )
    uploads: list[dict[str, Any]] = []
    total_bytes = 0
    for idx, (raw_path, req) in enumerate(zip(file_paths, requirements), 1):
        p = Path(raw_path).expanduser()
        try:
            before = os.lstat(p)
        except OSError as e:
            raise ValueError(f"第 {idx} 个上传文件不可读：{raw_path}") from e
        if os.path.islink(p) or not p.is_file():
            raise ValueError(f"第 {idx} 个上传文件必须是普通文件（不允许符号链接）：{raw_path}")
        if before.st_size <= 0:
            raise ValueError(f"第 {idx} 个上传文件为空：{raw_path}")
        if before.st_size > MAX_UPLOAD_FILE_BYTES:
            raise ValueError(f"第 {idx} 个上传文件超过 100 MiB 本地安全上限：{raw_path}")
        try:
            data = p.read_bytes()
        except OSError as e:
            raise ValueError(f"读取第 {idx} 个上传文件失败：{raw_path}") from e
        try:
            after = os.lstat(p)
        except OSError as e:
            raise ValueError(f"第 {idx} 个上传文件在读取期间消失：{raw_path}") from e
        if (
            os.path.islink(p)
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != len(data)
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise ValueError(f"第 {idx} 个上传文件在读取期间发生了变化：{raw_path}")
        total_bytes += len(data)
        if total_bytes > MAX_UPLOAD_TOTAL_BYTES:
            raise ValueError("所有上传文件总大小超过 100 MiB 上限")
        filename = p.name
        ctype = UPLOAD_CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
        uploads.append({
            "key": req["key"],
            "requirement_name": req["name"],
            "requirement_type": req["type"],
            "path": str(p),
            "filename": filename,
            "content_type": ctype,
            "size": len(data),
            "bytes": data,
        })
    return uploads


def _encode_form_data(
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, str, bytes]],
) -> tuple[str, bytes]:
    boundary = "----MonashStudyKitOnTrack" + secrets.token_hex(12)
    chunks: list[bytes] = []
    for name, val in fields:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(val.encode("utf-8"))
        chunks.append(b"\r\n")
    for key, filename, ctype, payload in files:
        safe_name = filename.replace('"', "_").replace("\r", "").replace("\n", "")
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{key}"; filename="{safe_name}"\r\n'.encode())
        chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode())
        chunks.append(payload)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


# ---------------------------------------------------------------- OnTrack API 客户端

class OnTrackClient:
    """OnTrack / Doubtfire HTTP 客户端，带自动 401/419 续期重试与只读/写接口封装。"""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        session: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        delay: float = REQUEST_GAP,
    ):
        self.session = session or resolve_session(base_url)
        self.base_url = normalize_base_url(self.session["base_url"])
        self.timeout = timeout
        self.delay = delay
        self._last_req = 0.0
        self._projects_cache: dict[bool, list[dict[str, Any]]] = {}

    def _pace(self) -> None:
        if self.delay <= 0:
            return
        wait = self.delay - (time.monotonic() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.monotonic()

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        full = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            params = []
            for k, v in query.items():
                if v is None:
                    continue
                if isinstance(v, bool):
                    params.append((k, "true" if v else "false"))
                else:
                    params.append((k, str(v)))
            if params:
                full += "?" + urllib.parse.urlencode(params)
        return full

    def _send(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        limit: int | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        url = self._url(path, query)
        upper = method.upper()
        for attempt in range(2):
            self._pace()
            tok = self.session["access_token"]
            req_headers = {
                "Accept": "application/json",
                "Username": self.session["username"],
                "Auth-Token": tok,
                **(headers or {}),
            }
            status, resp_headers, data = _http_send(
                url,
                method=upper,
                headers=req_headers,
                body=body,
                timeout=self.timeout,
                limit=limit,
                secret_values=(tok,),
            )
            if status in (401, 419) and upper == "GET" and attempt == 0 and self.session.get("provenance") != "environment":
                self.session = resolve_session(self.base_url, force_refresh=True, rejected_token=tok)
                continue
            if status in (401, 419):
                raise OnTrackAuthError("OnTrack 拒绝了当前登录会话，请运行 monash ontrack login 重新登录。", status_code=status)
            if status == 404:
                raise OnTrackNotFoundError("OnTrack 中找不到请求的课程或任务")
            if status < 200 or status >= 300:
                detail = _upstream_error_detail(data)
                if detail:
                    detail = _redact(detail, tok)
                raise OnTrackError(
                    f"OnTrack 返回 HTTP {status}" + (f"：{detail}" if detail else ""),
                    status_code=status,
                )
            return status, resp_headers, data
        raise OnTrackAuthError("刷新后的 OnTrack 会话仍被拒绝", status_code=419)

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, Any]:
        status, _, data = self._send(path, method=method, query=query, headers=headers, body=body)
        try:
            return status, json.loads(data.decode("utf-8"))
        except ValueError as e:
            raise OnTrackError("OnTrack 返回了无效的 JSON 响应") from e

    def download_file(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_headers = {"Accept": "application/octet-stream"}
        status, hdrs, first = self._send(path, query=query, headers=base_headers, limit=MAX_DOWNLOAD_BYTES)
        ctype = (hdrs.get("content-type") or "").split(";", 1)[0].strip() or None
        fname = _response_filename(hdrs.get("content-disposition"))
        if status != 206:
            return {"bytes": first, "content_type": ctype, "filename": fname}

        cr = _content_range(hdrs.get("content-range"))
        if not cr or cr[0] != 0 or len(first) != (cr[1] - cr[0] + 1):
            raise OnTrackError("OnTrack 返回了无效的 Content-Range 响应")
        total = cr[2]
        if total > MAX_DOWNLOAD_BYTES:
            raise OnTrackError("OnTrack 下载超过 256 MiB 上限")
        buf = bytearray(total)
        buf[0:len(first)] = first
        offset = cr[1] + 1
        while offset < total:
            r_status, r_hdrs, part = self._send(
                path,
                query=query,
                headers={**base_headers, "Range": f"bytes={offset}-"},
                limit=MAX_DOWNLOAD_BYTES,
            )
            r_cr = _content_range(r_hdrs.get("content-range"))
            if r_status != 206 or not r_cr or r_cr[0] != offset or r_cr[2] != total or len(part) != (r_cr[1] - r_cr[0] + 1):
                raise OnTrackError("OnTrack 分片下载返回的 Content-Range 无效")
            buf[offset:offset + len(part)] = part
            offset = r_cr[1] + 1
        return {"bytes": bytes(buf), "content_type": ctype, "filename": fname}

    # ---- 只读接口 ----

    def auth_method(self) -> dict[str, Any]:
        return discover_auth_method(self.base_url, timeout=self.timeout)

    def projects(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        if include_inactive not in self._projects_cache:
            _, raw = self._request_json("api/projects", query={"include_inactive": include_inactive})
            self._projects_cache[include_inactive] = parse_projects(raw)
        return self._projects_cache[include_inactive]

    def project(self, project_id: int) -> dict[str, Any]:
        try:
            _, raw = self._request_json(f"api/projects/{project_id}")
        except OnTrackError as e:
            if e.status_code == 403:
                raise OnTrackError(
                    f"无权访问项目 {project_id}（项目 ID 请使用 monash ontrack courses --include-inactive 显示的 id）",
                    status_code=403,
                ) from e
            raise
        return parse_project(raw)

    def unit(self, unit_id: int) -> dict[str, Any]:
        _, raw = self._request_json(f"api/units/{unit_id}")
        return parse_unit(raw)

    def roles(self, active_only: bool = True) -> list[dict[str, Any]]:
        _, raw = self._request_json("api/unit_roles", query={"active_only": active_only})
        return parse_roles(raw)

    def download_unit_resources(self, project_id: int) -> dict[str, Any]:
        proj = self.project(project_id)
        unit_id = proj["unit"]["id"]
        dl = self.download_file(f"api/units/{unit_id}/all_resources")
        if not is_valid_zip(dl["bytes"]):
            raise OnTrackError("OnTrack 返回的课程资源压缩包不是有效的 ZIP 文件")
        return {
            "project_id": project_id,
            "unit_id": unit_id,
            "unit_code": proj["unit"]["code"],
            "filename": dl["filename"] or f"{_safe_filename(proj['unit']['code'])}-resources.zip",
            "bytes": dl["bytes"],
        }

    def download_task_sheet(self, unit_id: int, task_definition_id: int) -> dict[str, Any]:
        dl = self.download_file(
            f"api/units/{unit_id}/task_definitions/{task_definition_id}/task_pdf",
            query={"as_attachment": True},
        )
        if (dl.get("filename") or "").lower() == "filenotfound.pdf":
            raise OnTrackError("该任务没有任务书 PDF（task sheet）")
        if dl.get("content_type") != "application/pdf" or not is_valid_pdf(dl["bytes"]):
            raise OnTrackError("OnTrack 返回的任务书不是有效的 PDF 文件")
        return dl

    def download_task_resources(self, unit_id: int, task_definition_id: int) -> dict[str, Any]:
        return self.download_file(
            f"api/units/{unit_id}/task_definitions/{task_definition_id}/task_resources"
        )

    # ---- 有副作用 / 写接口 ----

    def task_comments(self, project_id: int, task_definition_id: int) -> list[dict[str, Any]]:
        """注意：Doubtfire 服务端在拉取评论历史时会顺带把非讨论类消息标为已读。"""
        _, raw = self._request_json(f"api/projects/{project_id}/task_def_id/{task_definition_id}/comments")
        if not isinstance(raw, list):
            raise OnTrackError("task comments 必须是列表")
        return [parse_task_comment(c) for c in raw]

    def update_task_state(self, project_id: int, task_definition_id: int, state: str) -> dict[str, Any]:
        if state not in WRITABLE_TASK_STATES:
            raise ValueError(f"状态必须是：{', '.join(WRITABLE_TASK_STATES)}")
        payload = json.dumps({"trigger": state}).encode("utf-8")
        _, raw = self._request_json(
            f"api/projects/{project_id}/task_def_id/{task_definition_id}",
            method="PUT",
            headers={"Content-Type": "application/json"},
            body=payload,
        )
        if not isinstance(raw, dict):
            raise OnTrackError("update_task_state 返回了无效对象")
        return {
            "id": _req_int(raw.get("id"), "task_update.id"),
            "task_definition_id": _req_int(raw.get("task_definition_id"), "task_update.task_definition_id"),
            "status": _req_str(raw.get("status"), "task_update.status"),
        }

    def add_task_comment(self, project_id: int, task_definition_id: int, message: str) -> dict[str, Any]:
        body = urllib.parse.urlencode({"comment": message}).encode("utf-8")
        _, raw = self._request_json(
            f"api/projects/{project_id}/task_def_id/{task_definition_id}/comments",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            body=body,
        )
        return parse_task_comment(raw)

    def submit_task(
        self,
        project_id: int,
        task_definition_id: int,
        uploads: list[dict[str, Any]],
        *,
        submission_type: str = "ready_for_feedback",
        comment: str | None = None,
        accept_tii_eula: bool = False,
    ) -> dict[str, Any]:
        if submission_type not in SUBMISSION_TYPES:
            raise ValueError(f"提交类型必须是：{', '.join(SUBMISSION_TYPES)}")
        fields = [("trigger", submission_type)]
        if comment is not None:
            fields.append(("comment", comment))
        if accept_tii_eula:
            fields.append(("accepted_tii_eula", "true"))
        files = [(u["key"], u["filename"], u["content_type"], u["bytes"]) for u in uploads]
        ctype, body = _encode_form_data(fields, files)
        status, raw = self._request_json(
            f"api/projects/{project_id}/task_def_id/{task_definition_id}/submission",
            method="POST",
            headers={"Content-Type": ctype},
            body=body,
        )
        if status != 201:
            raise OnTrackError(f"OnTrack 提交返回 HTTP {status}（预期 201）", status_code=status)
        if not isinstance(raw, dict):
            raise OnTrackError("submit_task 返回了无效对象")
        return {
            "id": _req_int(raw.get("id"), "task_update.id"),
            "task_definition_id": _req_int(raw.get("task_definition_id"), "task_update.task_definition_id"),
            "status": _req_str(raw.get("status"), "task_update.status"),
        }


# ---------------------------------------------------------------- 高层服务操作

def _matching_projects(projects: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.strip().lower()
    exact = [p for p in projects if p["unit"]["code"].lower() == q]
    if exact:
        return exact
    return [
        p for p in projects
        if q in p["unit"]["code"].lower() or q in p["unit"]["name"].lower()
    ]


def resolve_project_id(client: OnTrackClient, reference: str) -> int:
    ref = (reference or "").strip()
    if not ref:
        raise ValueError("请提供课程代码、名称或项目 ID（运行 monash ontrack courses 查看）")
    if ref.isdigit():
        val = int(ref)
        if val <= 0:
            raise ValueError("项目 ID 必须是正整数")
        return val
    active = _matching_projects(client.projects(include_inactive=False), ref)
    if len(active) == 1:
        return active[0]["id"]
    if len(active) > 1:
        ids = ", ".join(str(p["id"]) for p in sorted(active, key=lambda x: x["id"]))
        raise ValueError(f"课程 {ref} 匹配到多个项目：{ids}，请改用项目 ID")
    all_projects = client.projects(include_inactive=True)
    matched = _matching_projects(all_projects, ref)
    if len(matched) == 1:
        return matched[0]["id"]
    if len(matched) > 1:
        ids = ", ".join(str(p["id"]) for p in sorted(matched, key=lambda x: x["id"]))
        raise ValueError(f"课程 {ref} 匹配到多个项目：{ids}，请改用项目 ID")
    known = ", ".join(sorted({p["unit"]["code"] for p in all_projects})) or "无"
    raise OnTrackNotFoundError(f"找不到匹配 {ref} 的 OnTrack 课程（当前课程：{known}）")


def project_snapshot(client: OnTrackClient, project_ref: str | int) -> dict[str, Any]:
    pid = project_ref if isinstance(project_ref, int) else resolve_project_id(client, str(project_ref))
    proj = client.project(pid)
    unit = client.unit(proj["unit"]["id"])
    return build_project_snapshot(proj, unit)


def select_task(snapshot: dict[str, Any], task_ref: str) -> tuple[dict[str, Any], dict[str, Any]]:
    norm = (task_ref or "").strip().lower()
    if not norm:
        raise ValueError("请提供任务编号（如 1.1P）或 task_definition_id")
    row = next((t for t in snapshot["tasks"] if t["abbreviation"].lower() == norm), None)
    if row is None and norm.isdigit():
        tid = int(norm)
        row = next((t for t in snapshot["tasks"] if t["task_definition_id"] == tid), None)
    if row is None:
        raise OnTrackNotFoundError(
            f"项目 {snapshot['project']['id']} 中找不到任务 {task_ref}（运行 monash ontrack tasks 查看列表）"
        )
    td = next((d for d in snapshot["unit"]["task_definitions"] if d["id"] == row["task_definition_id"]), None)
    if td is None:
        raise OnTrackNotFoundError(f"找不到任务 {task_ref} 的定义")
    return row, td


def user_info(client: OnTrackClient) -> dict[str, Any]:
    method = client.auth_method()
    # 拉一次受保护接口，验证会话仍然有效
    client.projects(include_inactive=True)
    sess = client.session
    base = {"username": sess["username"], "base_url": client.base_url, "auth_method": method["method"]}
    if isinstance(sess.get("user"), dict):
        return _sanitize_value({**sess["user"], **base})
    return base


def auth_check(client: OnTrackClient) -> dict[str, Any]:
    method = client.auth_method()
    projects = client.projects(include_inactive=True)
    roles = client.roles(active_only=False)
    return _sanitize_value({
        "base_url": client.base_url,
        "username": client.session["username"],
        "auth_method": method["method"],
        "projects": len(projects),
        "unit_roles": len(roles),
        "cached_user": client.session.get("user"),
    })


def list_tasks(client: OnTrackClient, unit_ref: str, statuses: list[str] | None = None) -> list[dict[str, Any]]:
    snap = project_snapshot(client, unit_ref)
    rows = snap["tasks"]
    if statuses:
        wanted = {s.strip().lower() for s in statuses if s.strip()}
        rows = [t for t in rows if t["status"].lower() in wanted]
    return rows


def task_detail(client: OnTrackClient, unit_ref: str, task_ref: str) -> dict[str, Any]:
    snap = project_snapshot(client, unit_ref)
    row, td = select_task(snap, task_ref)
    return {
        **row,
        "project_id": snap["project"]["id"],
        "unit_code": snap["unit"]["code"],
        "unit_name": snap["unit"]["name"],
        "description": td.get("description"),
        "has_task_sheet": td.get("has_task_sheet"),
        "has_task_resources": td.get("has_task_resources"),
        "upload_requirements": td.get("upload_requirements", []),
    }


def list_chats(client: OnTrackClient, unit_ref: str) -> list[dict[str, Any]]:
    """只读列出某门课每个任务的未读导师消息数（不标已读）。"""
    snap = project_snapshot(client, unit_ref)
    return [
        {
            "task_definition_id": row["task_definition_id"],
            "task": row["abbreviation"],
            "name": row["name"],
            "status": row["status"],
            "status_label": row["status_label"],
            "unread_comments": row["unread_comments"],
        }
        for row in snap["tasks"]
    ]


def read_task_sheet_markdown(
    client: OnTrackClient,
    unit_ref: str,
    task_ref: str,
    *,
    cache_pdf: bool = True,
) -> dict[str, Any]:
    """下载（或读本地缓存的）任务书 PDF，并用 textextract（pypdf / pdftotext）提取为 Markdown。"""
    snap = project_snapshot(client, unit_ref)
    row, td = select_task(snap, task_ref)
    if td.get("has_task_sheet") is False:
        raise OnTrackError(f"任务 {row['abbreviation']} 没有任务书 PDF")
    unit_code = snap["unit"]["code"]
    filename = f"{_safe_filename(unit_code)}-{_safe_filename(row['abbreviation'])}.pdf"
    dl = client.download_task_sheet(snap["unit"]["id"], td["id"])
    pdf_bytes = dl["bytes"]

    saved_path: Path | None = None
    if cache_pdf:
        target_dir = SHEETS_DIR / _safe_filename(unit_code)
        ensure_private_dir(target_dir)
        saved_path = target_dir / filename
        tmp = saved_path.with_suffix(".pdf.tmp")
        tmp.write_bytes(pdf_bytes)
        os.replace(tmp, saved_path)
        pages = textextract.pdf_pages(saved_path)
    else:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tmp_path = Path(tf.name)
        try:
            pages = textextract.pdf_pages(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    parts = [f"# {unit_code} {row['abbreviation']} — {row['name']}\n"]
    if any(p.strip() for p in pages):
        for idx, text in enumerate(pages, 1):
            body = text.strip()
            if body:
                parts.append(f"## Page {idx}\n\n{body}")
    else:
        parts.append("（未能从该 PDF 提取文字，可能是扫描件图片；可用 `monash ontrack get` 下载原 PDF 查看。）")

    return {
        "project_id": snap["project"]["id"],
        "unit_id": snap["unit"]["id"],
        "unit_code": unit_code,
        "task_definition_id": td["id"],
        "task": row["abbreviation"],
        "name": row["name"],
        "filename": filename,
        "pages": len(pages),
        "cached_path": str(saved_path) if saved_path else None,
        "markdown": "\n\n".join(parts) + "\n",
    }


def save_download(dest: Path, data: bytes, *, force: bool = False) -> Path:
    dest = dest.expanduser()
    if dest.exists() and not force:
        raise ValueError(f"目标文件已存在：{dest}（加 --force 覆盖）")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


# ---------------------------------------------------------------- 本地缓存与 todo / semester 整合

def load_cache() -> dict[str, Any] | None:
    if not CACHE_FILE.is_file():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def sync(client: OnTrackClient | None = None, *, download_sheets: bool = False) -> dict[str, Any]:
    """同步当前活跃 OnTrack 课程和任务快照到本地缓存（DATA_DIR/ontrack/snapshot.json）。"""
    cl = client or OnTrackClient()
    active_projects = cl.projects(include_inactive=False)
    snapshots = []
    sheets_downloaded = 0
    errors: list[str] = []

    for p in active_projects:
        try:
            snap = project_snapshot(cl, p["id"])
            snapshots.append({
                "project_id": snap["project"]["id"],
                "unit_id": snap["unit"]["id"],
                "unit_code": snap["unit"]["code"],
                "unit_name": snap["unit"]["name"],
                "target_grade": snap["project"].get("target_grade"),
                "target_grade_label": grade_label(
                    snap["project"].get("target_grade"), snap["unit"].get("grade_definitions", [])
                ),
                "tasks": snap["tasks"],
            })
            if download_sheets:
                for td in snap["unit"].get("task_definitions", []):
                    if td.get("has_task_sheet") is False:
                        continue
                    try:
                        dl = cl.download_task_sheet(snap["unit"]["id"], td["id"])
                        target_dir = SHEETS_DIR / _safe_filename(snap["unit"]["code"])
                        ensure_private_dir(target_dir)
                        fname = f"{_safe_filename(snap['unit']['code'])}-{_safe_filename(td['abbreviation'])}.pdf"
                        save_download(target_dir / fname, dl["bytes"], force=True)
                        sheets_downloaded += 1
                    except OnTrackError:
                        pass
        except OnTrackError as e:
            errors.append(f"{p['unit']['code']}: {e}")

    payload = {
        "synced_at": _iso_utc(_now_utc()),
        "base_url": cl.base_url,
        "username": cl.session["username"],
        "projects": snapshots,
        "errors": errors,
    }
    ensure_private_dir(CACHE_DIR)
    local_state.atomic_write(CACHE_FILE, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {
        "synced_at": payload["synced_at"],
        "units": len(snapshots),
        "tasks": sum(len(s["tasks"]) for s in snapshots),
        "sheets_downloaded": sheets_downloaded,
        "errors": errors,
    }


def todo_summary(days: int = 7, *, today: date | None = None) -> dict[str, Any] | None:
    """读取本地 OnTrack 快照，整理接下来 N 天到期、已逾期/需跟进和有未读导师回复的任务。

    只在 ontrack_enabled=True 且本地有缓存时返回；不发网络请求，不影响离线速度。
    """
    if not load_settings().get("ontrack_enabled"):
        return None
    cache = load_cache()
    if not cache:
        return {"synced_at": None, "due": [], "attention": [], "unread_chats": [], "missing_cache": True}

    ref_today = today or _local_today()
    horizon = ref_today + timedelta(days=days)
    due: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    unread_chats: list[dict[str, Any]] = []

    for proj in cache.get("projects", []):
        code = proj.get("unit_code") or "?"
        pid = proj.get("project_id")
        for t in proj.get("tasks", []):
            status = t.get("status") or ""
            final = is_final_status(status)
            deadline_str = t.get("deadline") or t.get("due_date")
            dl_date = _parse_civil_date(deadline_str) if deadline_str else None
            base_item = {
                "course": code,
                "project_id": pid,
                "task": t.get("abbreviation"),
                "name": t.get("name"),
                "status": status,
                "status_label": t.get("status_label") or status_label(status),
                "deadline": deadline_str,
                "source": "ontrack",
            }
            if not final and dl_date is not None:
                if ref_today <= dl_date <= horizon:
                    due.append(base_item)
                elif dl_date < ref_today:
                    attention.append({**base_item, "reason": "overdue"})
            if status in ACTION_REQUIRED_STATUSES:
                if not any(a["project_id"] == pid and a["task"] == base_item["task"] for a in attention):
                    attention.append({**base_item, "reason": status})
            unread = int(t.get("unread_comments") or 0)
            if unread > 0:
                unread_chats.append({**base_item, "unread_comments": unread})

    due.sort(key=lambda x: (x["deadline"] or "9999-99-99", x["course"], x["task"] or ""))
    attention.sort(key=lambda x: (x["deadline"] or "9999-99-99", x["course"], x["task"] or ""))
    unread_chats.sort(key=lambda x: (-x["unread_comments"], x["course"], x["task"] or ""))
    return {
        "synced_at": cache.get("synced_at"),
        "due": due,
        "attention": attention,
        "unread_chats": unread_chats,
        "missing_cache": False,
    }


def enable_after_login(base_url: str, *, allow_write: bool = False) -> None:
    updates: dict[str, Any] = {
        "ontrack_enabled": True,
        "ontrack_url": normalize_base_url(base_url),
    }
    if allow_write:
        updates["ontrack_write_enabled"] = True
    save_settings(updates)
