"""Opt-in single-user remote MCP with pairing, PKCE and rotating OAuth tokens.

Bind on loopback behind your own HTTPS reverse proxy. This is not a hosted or
multi-user service. Credentials and OAuth state stay in this user's own home.
Adapted from the private server's tested transport; no private deployment paths.
"""
from __future__ import annotations

from . import __version__
from .paths import SECRETS_DIR, ensure_private_dir

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PUBLIC_URL = os.environ.get("MONASH_KIT_REMOTE_URL", "http://127.0.0.1:8765").rstrip("/")
RESOURCE = PUBLIC_URL + "/mcp"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
PAIR_TTL = 600
PAIR_MAX_TRIES = 5
REDIRECT_OK = {
    ("claude.ai", "/api/mcp/auth_callback"),
    ("claude.com", "/api/mcp/auth_callback"),
    ("chatgpt.com", "/connector_platform_oauth_redirect"),
}
# ChatGPT 旧式 callback-ID 回调；只接受 chatgpt.com 的单段 ID，不接受任意跳转 URL。
CHATGPT_CALLBACK = re.compile(r"/connector/oauth/[A-Za-z0-9_-]{1,128}\Z")
MAX_BODY = 1 << 20
# 注册不需要认证（协议就是这么定的），不设上限的话谁都能一直往库里塞客户端
MAX_CLIENTS = 50
EXTRA_REDIRECTS: set[str] = set()
# monash = Moodle + Ed 只读。其余是可选的学生服务，配对时由用户逐项勾选，见 student_tools.REMOTE_CHOICES
SCOPES = ("monash", "gmail.read", "gmail.manage", "attendance.read", "attendance.submit")


def configure(public_url: str, redirects: list[str] | None = None) -> None:
    """An explicit HTTPS origin, or loopback HTTP for local acceptance testing."""
    global PUBLIC_URL, RESOURCE, EXTRA_REDIRECTS
    if any(ord(c) < 33 for c in public_url):
        raise ValueError("Public URL must not contain whitespace or control characters")
    p = urllib.parse.urlsplit(public_url)
    local_http = p.scheme == "http" and p.hostname in {"127.0.0.1", "localhost", "::1"}
    if (p.scheme != "https" and not local_http) or not p.hostname or p.username or p.password \
            or p.query or p.fragment or p.path not in {"", "/"}:
        raise ValueError("Use an HTTPS origin without a path, credentials, query or fragment")
    _ = p.port  # reject malformed ports before starting a server
    extra = set(redirects or [])
    for uri in extra:
        if any(ord(c) < 33 for c in uri):
            raise ValueError("Callback URL must not contain whitespace or control characters")
        callback = urllib.parse.urlsplit(uri)
        local = callback.scheme == "http" and callback.hostname in {"127.0.0.1", "localhost", "::1"} and callback.port
        if (callback.scheme != "https" and not local) or not callback.hostname or callback.username \
                or callback.password or callback.query or callback.fragment or "*" in uri:
            raise ValueError("Extra callbacks must be exact HTTPS or loopback HTTP URLs without credentials, query or fragment")
        _ = callback.port
    EXTRA_REDIRECTS = extra
    PUBLIC_URL = urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), "", "", ""))
    RESOURCE = PUBLIC_URL + "/mcp"


def handle_remote(msg: dict, scopes=frozenset({"monash"}), owner: str = "remote") -> dict | None:
    """scopes 是这个客户端配对时被授权的功能，owner 是客户端 ID（确认凭据和后台任务按它分开）。"""
    from . import mcp_server as mcp
    from . import student_tools
    granted = student_tools.remote_scopes(scopes, mcp.load_settings())
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32602, "message": "params must be an object"}}
    if msg.get("method") == "tools/call":
        name = params.get("name", "")
        if not isinstance(name, str) or not isinstance(params.get("arguments", {}), dict):
            return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32602, "message": "Invalid tool arguments"}}
        name = mcp.LEGACY_TOOL_ALIASES.get(name, name)
        if student_tools.remote_allowed(name, granted):
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "result": student_tools.remote_call(name, params.get("arguments") or {}, granted, owner)}
        if name in student_tools.NAMES or name in student_tools.REMOTE_NAMES:
            return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {
                "code": -32602, "message": "This client is not authorized for this tool; pair again and tick the feature"}}
        spec = mcp.TOOLS.get(name)
        if spec and len(spec) > 3 and not spec[3]:
            return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {
                "code": -32602, "message": "Run login or sync on the server's own terminal"}}
    reply = mcp.handle(msg)
    if reply and "result" in reply:
        if msg.get("method") == "tools/list":
            reply["result"]["tools"] = [
                {**t, "securitySchemes": [{"type": "oauth2", "scopes": ["monash"]}]}
                for t in reply["result"]["tools"] if t["annotations"]["readOnlyHint"] and t["name"] not in student_tools.NAMES]
            reply["result"]["tools"] += student_tools.remote_tool_list(granted)
        elif msg.get("method") == "initialize":
            reply["result"]["instructions"] = (
                "Monash Moodle + Ed，只读。get_study_todo 看待办；search_content 搜资料；"
                "get_ed_lessons 看进度或练习题。登录过期请用户在服务器自己的终端重新登录；"
                "令牌不要发进聊天。不能提交作业、交卷或发帖。" + student_tools.remote_hint(granted))
    return reply



def _now() -> int:
    return int(time.time())


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def db(path: Path | None = None) -> sqlite3.Connection:
    if path is None:
        path = SECRETS_DIR / "remote-mcp.db"
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    con = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, name TEXT, redirect_uris TEXT, created INTEGER);
    CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT,
        challenge TEXT, expires INTEGER, resource TEXT NOT NULL DEFAULT '', scopes TEXT NOT NULL DEFAULT 'monash');
    CREATE TABLE IF NOT EXISTS tokens (hash TEXT PRIMARY KEY, kind TEXT, client_id TEXT, expires INTEGER,
        created INTEGER, resource TEXT NOT NULL DEFAULT '', scopes TEXT NOT NULL DEFAULT 'monash');
    CREATE TABLE IF NOT EXISTS pairing (id INTEGER PRIMARY KEY CHECK (id = 1), hash TEXT, expires INTEGER,
        tries INTEGER);
    """)
    # Keep public self-hosted token state across future schema upgrades.
    for table in ("codes", "tokens"):
        columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
        if "resource" not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN resource TEXT NOT NULL DEFAULT ''")
        # 加这一列之前配对的客户端只有 Moodle + Ed，不会因为升级就能读邮箱
        if "scopes" not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN scopes TEXT NOT NULL DEFAULT 'monash'")
        con.execute(f"UPDATE {table} SET resource=? WHERE resource=''", (RESOURCE,))
    return con


# ---------------------------------------------------------------- 配对、令牌

def open_pairing(con) -> str:
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))  # 例：3F9A-0C21-B7E4
    con.execute("INSERT INTO pairing (id, hash, expires, tries) VALUES (1, ?, ?, 0) "
                "ON CONFLICT(id) DO UPDATE SET hash=excluded.hash, expires=excluded.expires, tries=0",
                (_h(code), _now() + PAIR_TTL))
    return code


def check_pairing(con, code: str) -> bool:
    """配对码对了就消耗掉（一次性）；错了记一次，满 5 次作废整个窗口。"""
    row = con.execute("SELECT hash, expires, tries FROM pairing WHERE id=1").fetchone()
    if not row or not row["hash"] or row["expires"] < _now():
        return False
    given = _h(code.strip().upper().replace(" ", ""))
    if hmac.compare_digest(given, row["hash"]):
        con.execute("UPDATE pairing SET hash=NULL WHERE id=1")
        return True
    tries = row["tries"] + 1
    con.execute("UPDATE pairing SET tries=?, hash=CASE WHEN ? >= ? THEN NULL ELSE hash END WHERE id=1",
                (tries, tries, PAIR_MAX_TRIES))
    return False


def pairing_open(con) -> bool:
    row = con.execute("SELECT hash, expires FROM pairing WHERE id=1").fetchone()
    return bool(row and row["hash"] and row["expires"] >= _now())


def prune_clients(con) -> bool:
    """满了就先清掉注册超过一小时、从来没换到过令牌的客户端；清完还是满的就返回 False。

    真在用的客户端一定有令牌（刷新令牌 30 天），不会被清掉。
    """
    if con.execute("SELECT COUNT(*) FROM clients").fetchone()[0] < MAX_CLIENTS:
        return True
    con.execute("DELETE FROM clients WHERE created < ? AND id NOT IN (SELECT client_id FROM tokens) "
                "AND id NOT IN (SELECT client_id FROM codes)", (_now() - 3600,))
    return con.execute("SELECT COUNT(*) FROM clients").fetchone()[0] < MAX_CLIENTS


def issue_tokens(con, client_id: str, resource: str | None = None, scopes: str = "monash") -> dict:
    resource = RESOURCE if resource is None else resource
    if resource != RESOURCE:
        raise ValueError("invalid_target")
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = _now()
    con.execute("INSERT INTO tokens (hash, kind, client_id, expires, created, resource, scopes) "
                "VALUES (?, 'access', ?, ?, ?, ?, ?)", (_h(access), client_id, now + ACCESS_TTL, now, resource, scopes))
    con.execute("INSERT INTO tokens (hash, kind, client_id, expires, created, resource, scopes) "
                "VALUES (?, 'refresh', ?, ?, ?, ?, ?)", (_h(refresh), client_id, now + REFRESH_TTL, now, resource, scopes))
    con.execute("DELETE FROM tokens WHERE expires < ?", (now,))
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": refresh, "scope": scopes}


def access_grant(con, token: str):
    """有效的访问令牌返回它的 client_id 和 scopes，否则 None。"""
    row = con.execute("SELECT client_id, scopes, expires, resource FROM tokens WHERE hash=? AND kind='access'",
                      (_h(token),)).fetchone()
    if row and row["expires"] >= _now() and hmac.compare_digest(row["resource"], RESOURCE):
        return row
    return None


def valid_access(con, token: str) -> bool:
    return access_grant(con, token) is not None


def pkce_ok(verifier: str, challenge: str) -> bool:
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return 43 <= len(verifier) <= 128 and hmac.compare_digest(digest, challenge)


def redirect_allowed(uri: str) -> bool:
    if uri in EXTRA_REDIRECTS:
        return True  # operator explicitly allowed this exact callback, never a pattern
    try:
        p = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    # Match the full authority, not just hostname: no credentials, ports, queries or fragments.
    if p.scheme != "https" or p.netloc not in {"claude.ai", "claude.com", "chatgpt.com"}:
        return False
    if p.query or p.fragment:
        return False
    return (p.netloc, p.path) in REDIRECT_OK or (
        p.netloc == "chatgpt.com" and CHATGPT_CALLBACK.fullmatch(p.path) is not None
    )


# ---------------------------------------------------------------- HTTP

AUTH_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Authorize Monash Study Kit</title><style>
:root{{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e4df;--accent:#0b62d6;--warn:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--card:#1f1f1d;--fg:#ecece8;--muted:#9a9a93;--line:#2f2f2c;--accent:#6aa7ff;--warn:#ffb4a8}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,"PingFang SC",sans-serif}}
main{{max-width:420px;margin:12vh auto;padding:24px 16px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}}
h1{{font-size:20px;margin:0 0 8px}}p{{color:var(--muted);margin:0 0 16px}}
input{{width:100%;box-sizing:border-box;font:600 22px ui-monospace,monospace;letter-spacing:2px;text-align:center;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg)}}
button{{width:100%;margin-top:14px;padding:12px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:600 16px inherit;cursor:pointer}}
.err{{color:var(--warn);margin:0 0 12px}}
fieldset{{border:1px solid var(--line);border-radius:10px;margin:0 0 16px;padding:10px 12px}}
legend{{color:var(--muted);font-size:14px;padding:0 4px}}label{{display:flex;gap:8px;margin:6px 0;font-size:15px}}
label input{{width:auto;margin-top:4px}}</style></head><body><main><div class="card">
<h1>授权 {client} 使用 Monash Study Kit</h1>
<p>Moodle 和 Ed 只读：截止日期、成绩、公告、课件。输入在终端里运行 <code>monash remote pair</code> 得到的配对码。</p>
{error}<form method="post" action="/oauth/authorize">{hidden}{choices}
<input name="pairing_code" autocomplete="one-time-code" placeholder="XXXX-XXXX-XXXX" required autofocus>
<button type="submit">授权</button></form></div></main></body></html>"""


def _offered() -> list[str]:
    """配对页上可以勾的功能：只列服务器主人在设置里开了的。"""
    from . import mcp_server, student_tools
    enabled = student_tools.scopes(mcp_server.load_settings())
    return [s for s in student_tools.REMOTE_CHOICES if s in enabled]


def _choices(form: dict) -> str:
    from .student_tools import REMOTE_CHOICES
    offered = _offered()
    if not offered:
        return ""
    # 第一次打开时只读的默认勾上、写操作默认不勾；配对码输错重来时保留用户刚才的选择
    first = "pairing_code" not in form
    boxes = "".join(
        f'<label><input type="checkbox" name="grant_{s}" value="1"'
        f'{" checked" if (form.get("grant_" + s) if not first else s.endswith(".read")) else ""}>'
        f'{html.escape(REMOTE_CHOICES[s])}</label>' for s in offered)
    return f'<fieldset><legend>这个软件还可以用（不勾就不给）</legend>{boxes}</fieldset>'


class Handler(BaseHTTPRequestHandler):
    server_version = "monash-mcp/" + __version__
    con: sqlite3.Connection  # 由 make_server 设置
    lock: threading.RLock

    def log_message(self, fmt, *args):  # 不记查询串，里面可能有 code；下载链接的签名也不记
        path = self.path.split("?", 1)[0]
        sys.stderr.write("%s %s\n" % (self.command, "/dl/…" if path.startswith("/dl/") else path))

    # ---- 输出
    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", headers: dict | None = None):
        self.send_response(status)
        if body or ctype:
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, headers: dict | None = None):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json", headers)

    def _oauth_error(self, status: int, error: str, desc: str = ""):
        self._json(status, {"error": error, **({"error_description": desc} if desc else {})})

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def _form(self) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(self._body().decode(), keep_blank_values=True).items()}

    # ---- 路由
    # 单用户服务：每个请求整体串行。共用的 SQLite 连接和 MoodleClient（限速、cookie）
    # 都不是线程安全的，串行最省事。锁是可重入的，里面的 with self.lock 不会死锁。
    def do_GET(self):
        if not self._origin_ok():
            return
        with self.lock:
            self._get()

    def do_POST(self):
        if not self._origin_ok():
            return
        with self.lock:
            self._post()

    def _origin_ok(self) -> bool:
        port = self.server.server_address[1]
        allowed = {urllib.parse.urlsplit(PUBLIC_URL).netloc,
                   f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in allowed or (origin and origin != PUBLIC_URL):
            self._json(403, {"error": "invalid_origin"})
            return False
        return True

    def _get(self):
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            return self._json(200, {"resource": RESOURCE, "authorization_servers": [PUBLIC_URL],
                                    "bearer_methods_supported": ["header"], "scopes_supported": list(SCOPES)})
        if path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp"):
            return self._json(200, {
                "issuer": PUBLIC_URL, "authorization_response_iss_parameter_supported": True,
                "authorization_endpoint": PUBLIC_URL + "/oauth/authorize",
                "token_endpoint": PUBLIC_URL + "/oauth/token", "registration_endpoint": PUBLIC_URL + "/oauth/register",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": list(SCOPES)})
        if path == "/oauth/authorize":
            return self._authorize_page(q)
        if path == "/mcp":
            return self._send(405, b"", "", {"Allow": "POST"})
        self._send(404, b"not found", "text/plain")

    def _post(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/oauth/register":
                return self._register()
            if path == "/oauth/authorize":
                return self._authorize_submit()
            if path == "/oauth/token":
                return self._token()
            if path == "/mcp":
                return self._mcp()
        except ValueError as e:
            return self._oauth_error(400, "invalid_request", str(e))
        self._send(404, b"not found", "text/plain")

    def do_DELETE(self):
        self._send(405, b"", "", {"Allow": "POST"})

    # ---- OAuth
    def _register(self):
        try:
            meta = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            return self._oauth_error(400, "invalid_client_metadata", "not JSON")
        if not isinstance(meta, dict):
            return self._oauth_error(400, "invalid_client_metadata", "Expected an object")
        uris = meta.get("redirect_uris") or []
        if not isinstance(uris, list) or not uris or len(uris) > 10 or not all(
            isinstance(u, str) and redirect_allowed(u) for u in uris
        ):
            return self._oauth_error(400, "invalid_redirect_uri", "Callback is not allowed by this server")
        cid = "monash_" + secrets.token_urlsafe(16)
        name = str(meta.get("client_name") or "MCP Client")[:80]
        with self.lock:
            if not prune_clients(self.con):
                return self._oauth_error(429, "temporarily_unavailable", "注册的客户端太多了，先 revoke 掉不用的")
            self.con.execute("INSERT INTO clients VALUES (?, ?, ?, ?)", (cid, name, json.dumps(uris), _now()))
        self._json(201, {"client_id": cid, "client_name": name, "redirect_uris": uris,
                         "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                         "response_types": ["code"], "client_id_issued_at": _now()})

    def _client(self, cid: str):
        return self.con.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()

    def _check_authz_params(self, q: dict):
        """返回 (错误信息, client)。redirect_uri 不对时绝不能跳过去——直接在本页报错。"""
        client = self._client(q.get("client_id", ""))
        if not client:
            return "未知的客户端，请在客户端重新添加 connector。", None
        if q.get("redirect_uri") not in json.loads(client["redirect_uris"]):
            return "回调地址和注册时不一致。", None
        if q.get("response_type") != "code":
            return "只支持 response_type=code。", None
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return "缺少 PKCE（S256）。", None
        if "resource" in q and q["resource"] != RESOURCE:
            return "OAuth resource 与 MCP 地址不一致。", None
        if not set(q.get("scope", "monash").split()) <= set(SCOPES):
            return "不支持请求的 scope。", None
        return None, client

    def _authorize_page(self, q: dict, error: str = ""):
        err, client = self._check_authz_params(q)
        if err:
            return self._send(400, f"<p>{html.escape(err)}</p>".encode(), "text/html")
        keep = ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method",
                "response_type", "scope", "resource")
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(q.get(k, ""))}">' for k in keep if k in q)
        with self.lock:
            open_ = pairing_open(self.con)
        if not error and not open_:
            error = "现在没有打开的配对窗口。先在终端运行 pair，再刷新本页。"
        page = AUTH_PAGE.format(client=html.escape(client["name"]), hidden=hidden, choices=_choices(q),
                                error=f'<p class="err">{html.escape(error)}</p>' if error else "")
        self._send(200 if not error else 403, page.encode(), "text/html",
                   {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
                     "form-action 'self' https://claude.ai https://claude.com https://chatgpt.com",
                    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})

    def _authorize_submit(self):
        f = self._form()
        err, client = self._check_authz_params(f)
        if err:
            return self._send(400, f"<p>{html.escape(err)}</p>".encode(), "text/html")
        with self.lock:
            ok = check_pairing(self.con, f.get("pairing_code", ""))
        if not ok:
            return self._authorize_page(f, "配对码不对，或者配对窗口已过期/已用掉。重新运行 pair 拿新码。")
        code = secrets.token_urlsafe(32)
        scopes = " ".join(["monash"] + [s for s in _offered() if f.get("grant_" + s)])
        with self.lock:
            self.con.execute("INSERT INTO codes (hash, client_id, redirect_uri, challenge, expires, resource, scopes) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (_h(code), client["id"], f["redirect_uri"], f["code_challenge"],
                              _now() + CODE_TTL, f.get("resource", RESOURCE), scopes))
        params = {"code": code, "iss": PUBLIC_URL,
                  **({"state": f["state"]} if f.get("state") else {})}
        sep = "&" if "?" in f["redirect_uri"] else "?"
        self._send(302, b"", "", {"Location": f["redirect_uri"] + sep + urllib.parse.urlencode(params)})

    def _token(self):
        f = self._form()
        grant = f.get("grant_type")
        with self.lock:
            if grant == "authorization_code":
                row = self.con.execute("SELECT * FROM codes WHERE hash=?", (_h(f.get("code", "")),)).fetchone()
                self.con.execute("DELETE FROM codes WHERE hash=? OR expires < ?", (_h(f.get("code", "")), _now()))
                if not row or row["expires"] < _now() or not self._client(row["client_id"]):
                    return self._oauth_error(400, "invalid_grant", "授权码无效或已过期")
                if f.get("client_id") and f["client_id"] != row["client_id"]:
                    return self._oauth_error(400, "invalid_grant", "client 不匹配")
                if f.get("redirect_uri") != row["redirect_uri"]:
                    return self._oauth_error(400, "invalid_grant", "redirect_uri 不匹配")
                if not pkce_ok(f.get("code_verifier", ""), row["challenge"]):
                    return self._oauth_error(400, "invalid_grant", "PKCE 校验失败")
                if f.get("resource", row["resource"]) != row["resource"]:
                    return self._oauth_error(400, "invalid_target", "resource 与授权时不一致")
                return self._json(200, issue_tokens(self.con, row["client_id"], row["resource"], row["scopes"]))
            if grant == "refresh_token":
                h = _h(f.get("refresh_token", ""))
                row = self.con.execute("SELECT * FROM tokens WHERE hash=? AND kind='refresh'", (h,)).fetchone()
                if not row or row["expires"] < _now() or not self._client(row["client_id"]):
                    return self._oauth_error(400, "invalid_grant", "刷新令牌无效")
                if f.get("client_id") and f["client_id"] != row["client_id"]:
                    return self._oauth_error(400, "invalid_grant", "client 不匹配")
                if f.get("resource", row["resource"]) != row["resource"]:
                    return self._oauth_error(400, "invalid_target", "resource 与刷新令牌不一致")
                self.con.execute("DELETE FROM tokens WHERE hash=?", (h,))  # 用一次就换新的
                return self._json(200, issue_tokens(self.con, row["client_id"], row["resource"], row["scopes"]))
        self._oauth_error(400, "unsupported_grant_type")

    # ---- MCP（Streamable HTTP，无状态，只回 JSON）
    def _mcp(self):
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        with self.lock:
            grant = access_grant(self.con, token) if token else None
        if grant is None:
            return self._json(401, {"error": "invalid_token"}, {"WWW-Authenticate":
                              f'Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource"'})
        try:
            payload = json.loads(self._body() or b"null")
        except json.JSONDecodeError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        if not isinstance(payload, dict):
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32600, "message": "Expected one JSON-RPC object"}})
        reply = handle_remote(payload, frozenset(grant["scopes"].split()), grant["client_id"])
        if reply is None:
            return self._send(202, b"", "")
        self._json(200, reply)



def make_server(addr: tuple[str, int], con: sqlite3.Connection) -> ThreadingHTTPServer:
    if addr[0] not in {"127.0.0.1", "localhost"}:
        raise ValueError("Bind remote MCP on loopback behind an HTTPS reverse proxy")
    handler = type("BoundHandler", (Handler,), {"con": con, "lock": threading.RLock()})
    server = ThreadingHTTPServer(addr, handler)
    server.timeout = 1
    return server


def command(args) -> int:
    """Called only by the explicit `monash remote` CLI entry point."""
    if args.action == "serve":
        url = args.public_url or os.environ.get("MONASH_KIT_REMOTE_URL")
        if not url:
            raise ValueError("Set --public-url https://your-host.example or MONASH_KIT_REMOTE_URL")
        configure(url, args.redirect_uri)
    con = db()
    try:
        if args.action == "serve":
            from . import mcp_server
            server = make_server(("127.0.0.1", args.port), con)
            if not args.no_background:
                threading.Thread(target=mcp_server._background, daemon=True).start()
            print(f"Remote MCP: {RESOURCE} (loopback port {server.server_port})", file=sys.stderr)
            try:
                server.serve_forever()
            finally:
                server.server_close()
        elif args.action == "pair":
            print(f"Pairing code: {open_pairing(con)}\nValid for 10 minutes, single use.")
        elif args.action == "clients":
            for row in con.execute("SELECT id, name, (SELECT scopes FROM tokens WHERE client_id=clients.id "
                                   "ORDER BY created DESC LIMIT 1) AS scopes FROM clients ORDER BY created"):
                print(f"{row['id']}  {row['name']}  {row['scopes'] or '（还没拿到令牌）'}")
        elif args.action == "revoke":
            if args.all:
                con.executescript("DELETE FROM tokens; DELETE FROM codes; DELETE FROM clients; UPDATE pairing SET hash=NULL;")
            elif args.client:
                for table in ("tokens", "codes"):
                    con.execute(f"DELETE FROM {table} WHERE client_id=?", (args.client,))
                con.execute("DELETE FROM clients WHERE id=?", (args.client,))
            else:
                raise ValueError("Choose --client ID or --all")
            print("Authorization revoked")
    finally:
        con.close()
    return 0


