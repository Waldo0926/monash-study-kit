"""远程 MCP 的 OAuth + MCP 全流程：照 claude.ai 连 custom connector 的顺序走一遍。

发现（401 → resource metadata → AS metadata）→ 动态注册 → 授权页输配对码 →
授权码 + PKCE 换令牌 → 带令牌调 /mcp → 刷新令牌轮换。外加各种该拒绝的情况。
"""
import base64
import hashlib
import json
import os
import secrets
from types import SimpleNamespace
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from monash_study_kit import remote as R

CB = "https://claude.ai/api/mcp/auth_callback"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoRedirect)


@pytest.fixture
def server(tmp_path):
    con = R.db(tmp_path / "r.db")
    srv = R.make_server(("127.0.0.1", 0), con)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", con
    srv.shutdown()
    srv.server_close()
    con.close()


def req(url, method="GET", data=None, headers=None, form=None):
    body = None
    headers = dict(headers or {})
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        resp = opener.open(r)
    except urllib.error.HTTPError as e:
        resp = e
    raw = resp.read()
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw.decode()
    return resp.status if hasattr(resp, "status") else resp.code, resp.headers, parsed


def pkce():
    v = secrets.token_urlsafe(48)
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def connect(base, con, pair=True):
    """走完注册 + 授权 + 换令牌，返回 (client_id, tokens)。"""
    _, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [CB], "client_name": "Claude"})
    cid = reg["client_id"]
    v, c = pkce()
    code_pair = R.open_pairing(con) if pair else "0000-0000-0000"
    params = {"client_id": cid, "redirect_uri": CB, "response_type": "code", "state": "st8",
              "code_challenge": c, "code_challenge_method": "S256"}
    st, h, _ = req(base + "/oauth/authorize", "POST", form={**params, "pairing_code": code_pair})
    assert st == 302, st
    loc = urllib.parse.urlsplit(h["Location"])
    q = dict(urllib.parse.parse_qsl(loc.query))
    assert (loc.scheme + "://" + loc.netloc + loc.path) == CB and q["state"] == "st8"
    st, _, tok = req(base + "/oauth/token", "POST", form={"grant_type": "authorization_code", "code": q["code"],
                                                           "redirect_uri": CB, "client_id": cid, "code_verifier": v})
    assert st == 200, tok
    return cid, tok


def test_discovery_starts_from_401(server):
    base, _ = server
    st, h, _ = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert st == 401 and "resource_metadata=" in h["WWW-Authenticate"]
    _, _, prm = req(base + "/.well-known/oauth-protected-resource")
    assert prm["resource"].endswith("/mcp") and prm["authorization_servers"]
    _, _, asm = req(base + "/.well-known/oauth-authorization-server")
    assert asm["code_challenge_methods_supported"] == ["S256"] and asm["registration_endpoint"].endswith("/oauth/register")


def test_full_flow_then_tools(server):
    base, con = server
    cid, tok = connect(base, con)
    auth = {"Authorization": "Bearer " + tok["access_token"]}
    st, _, init = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                               "params": {"protocolVersion": "2025-06-18"}}, auth)
    assert st == 200 and init["result"]["serverInfo"]["name"] == "monash"
    st, _, _ = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, auth)
    assert st == 202
    _, _, tl = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, auth)
    names = {t["name"] for t in tl["result"]["tools"]}
    assert "list_moodle_due" in names and not any("submit" in n or n.startswith("moodle_quiz") for n in names)


def test_refresh_rotates(server):
    base, con = server
    cid, tok = connect(base, con)
    st, _, new = req(base + "/oauth/token", "POST", form={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    assert st == 200 and new["refresh_token"] != tok["refresh_token"]
    st, _, err = req(base + "/oauth/token", "POST", form={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    assert st == 400 and err["error"] == "invalid_grant"  # 旧的用过就作废


def test_register_rejects_foreign_redirects(server):
    base, _ = server
    for bad in ["https://evil.example/api/mcp/auth_callback", "http://claude.ai/api/mcp/auth_callback",
                "https://claude.ai/other", "https://claude.ai.evil.example/api/mcp/auth_callback"]:
        st, _, err = req(base + "/oauth/register", "POST", {"redirect_uris": [bad]})
        assert st == 400 and err["error"] == "invalid_redirect_uri", bad


def test_authorize_without_pairing_window_is_refused(server):
    base, con = server
    _, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [CB]})
    _, c = pkce()
    params = {"client_id": reg["client_id"], "redirect_uri": CB, "response_type": "code",
              "code_challenge": c, "code_challenge_method": "S256"}
    st, _, page = req(base + "/oauth/authorize?" + urllib.parse.urlencode(params))
    assert st == 403 and "没有打开的配对窗口" in page
    st, h, _ = req(base + "/oauth/authorize", "POST", form={**params, "pairing_code": "AAAA-BBBB-CCCC"})
    assert st == 403 and "Location" not in h


def test_pairing_code_is_single_use_and_locks_after_5_misses(tmp_path):
    con = R.db(tmp_path / "p.db")
    code = R.open_pairing(con)
    assert R.check_pairing(con, code.lower())  # 大小写不敏感
    assert not R.check_pairing(con, code)      # 用过就没了
    code = R.open_pairing(con)
    for _ in range(5):
        assert not R.check_pairing(con, "0000-0000-0000")
    assert not R.check_pairing(con, code)      # 错满 5 次，对的也不行了


def test_redirect_uri_must_match_registration(server):
    base, con = server
    _, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [CB]})
    _, c = pkce()
    R.open_pairing(con)
    st, h, _ = req(base + "/oauth/authorize", "POST", form={
        "client_id": reg["client_id"], "redirect_uri": "https://claude.com/api/mcp/auth_callback",
        "response_type": "code", "code_challenge": c, "code_challenge_method": "S256", "pairing_code": "x"})
    assert st == 400 and "Location" not in h


def test_token_needs_right_verifier_and_code_is_single_use(server):
    base, con = server
    _, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [CB]})
    v, c = pkce()
    code = R.open_pairing(con)
    _, h, _ = req(base + "/oauth/authorize", "POST", form={
        "client_id": reg["client_id"], "redirect_uri": CB, "response_type": "code",
        "code_challenge": c, "code_challenge_method": "S256", "pairing_code": code})
    auth_code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(h["Location"]).query))["code"]
    st, _, err = req(base + "/oauth/token", "POST", form={"grant_type": "authorization_code", "code": auth_code,
                                                           "redirect_uri": CB, "code_verifier": "x" * 50})
    assert st == 400 and "PKCE" in err["error_description"]
    st, _, _ = req(base + "/oauth/token", "POST", form={"grant_type": "authorization_code", "code": auth_code,
                                                         "redirect_uri": CB, "code_verifier": v})
    assert st == 400  # 失败一次授权码也已作废


def test_revoke_all_cuts_access(server):
    base, con = server
    _, tok = connect(base, con)
    con.executescript("DELETE FROM tokens; DELETE FROM clients;")
    st, _, _ = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   {"Authorization": "Bearer " + tok["access_token"]})
    assert st == 401


def test_authorize_page_escapes_input(server):
    base, con = server
    _, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [CB], "client_name": "<script>x</script>"})
    _, c = pkce()
    R.open_pairing(con)
    q = {"client_id": reg["client_id"], "redirect_uri": CB, "response_type": "code", "state": '"><img src=x>',
         "code_challenge": c, "code_challenge_method": "S256"}
    st, h, page = req(base + "/oauth/authorize?" + urllib.parse.urlencode(q))
    assert st == 200 and "<script>x" not in page and '"><img' not in page
    assert "default-src 'none'" in h["Content-Security-Policy"]


# ChatGPT OAuth regression: retain Claude compatibility while supporting RFC 9207 + resource binding.
GPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"


def test_chatgpt_callback_allowlist(server):
    base, _ = server
    for cb in (GPT_CB, "https://chatgpt.com/connector/oauth/aB12-__"):
        st, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [cb], "client_name": "ChatGPT"})
        assert st == 201 and cb in reg["redirect_uris"]
    for cb in ("https://chatgpt.com.evil.example/connector_platform_oauth_redirect",
               "https://evil.example@chatgpt.com:443/connector_platform_oauth_redirect",
               "https://chatgpt.com/connector/oauth/a/b",
               "https://chatgpt.com/connector/oauth/",
               "https://chatgpt.com/connector_platform_oauth_redirect?next=evil",
               "http://chatgpt.com/connector_platform_oauth_redirect"):
        st, _, err = req(base + "/oauth/register", "POST", {"redirect_uris": [cb]})
        assert st == 400 and err["error"] == "invalid_redirect_uri", cb


def test_chatgpt_full_oauth_flow_with_issuer_and_resource(server):
    base, con = server
    _, _, metadata = req(base + "/.well-known/oauth-authorization-server")
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["issuer"] == R.PUBLIC_URL
    _, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [GPT_CB]})
    cid = reg["client_id"]
    verifier, challenge = pkce()
    auth_params = {"client_id": cid, "redirect_uri": GPT_CB, "response_type": "code",
                   "code_challenge": challenge, "code_challenge_method": "S256",
                   "scope": "monash", "resource": R.RESOURCE, "state": "chatgpt-state"}
    pairing = R.open_pairing(con)
    st, hdr, _ = req(base + "/oauth/authorize", "POST",
                     form={**auth_params, "pairing_code": pairing})
    assert st == 302
    cb = urllib.parse.urlsplit(hdr["Location"])
    assert (cb.scheme + "://" + cb.netloc + cb.path) == GPT_CB
    q = dict(urllib.parse.parse_qsl(cb.query))
    assert q["iss"] == R.PUBLIC_URL and q["state"] == "chatgpt-state"
    token_params = {"grant_type": "authorization_code", "client_id": cid,
                    "code": q["code"], "redirect_uri": GPT_CB, "code_verifier": verifier,
                    "resource": R.RESOURCE}
    st, _, tok = req(base + "/oauth/token", "POST", form=token_params)
    assert st == 200 and tok["scope"] == "monash"
    row = con.execute("SELECT resource FROM tokens WHERE hash=?", (R._h(tok["access_token"]),)).fetchone()
    assert row["resource"] == R.RESOURCE
    st, _, result = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        {"Authorization": "Bearer " + tok["access_token"]})
    assert st == 200 and result["result"]["tools"]


def test_resource_scope_rejected_before_pairing_consumption(server):
    base, con = server
    _, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [GPT_CB]})
    _, challenge = pkce()
    params = {"client_id": reg["client_id"], "redirect_uri": GPT_CB, "response_type": "code",
              "code_challenge_method": "S256", "code_challenge": challenge}
    pairing = R.open_pairing(con)
    for extra in ({"resource": "https://evil.example/mcp"}, {"scope": "admin"}):
        st, h, _ = req(base + "/oauth/authorize", "POST",
                       form={**params, **extra, "pairing_code": pairing})
        assert st == 400 and "Location" not in h
    assert R.pairing_open(con)


def test_refresh_token_is_bound_to_resource_and_client(server):
    base, con = server
    cid, tok = connect(base, con)  # Existing Claude flow omits the resource parameter.
    f = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}
    st, _, err = req(base + "/oauth/token", "POST", form={**f, "resource": "https://evil.example/mcp"})
    assert st == 400 and err["error"] == "invalid_target"
    st, _, err = req(base + "/oauth/token", "POST", form={**f, "client_id": "another-client"})
    assert st == 400 and err["error"] == "invalid_grant"
    st, _, new = req(base + "/oauth/token", "POST", form={**f, "client_id": cid, "resource": R.RESOURCE})
    assert st == 200 and new["refresh_token"] != tok["refresh_token"]


def test_existing_claude_database_tokens_survive_migration(tmp_path):
    path = tmp_path / "legacy.db"
    con = R.sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE codes (hash TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT,
            challenge TEXT, expires INTEGER);
        CREATE TABLE tokens (hash TEXT PRIMARY KEY, kind TEXT, client_id TEXT,
            expires INTEGER, created INTEGER);
    """)
    old = "legacy-claude-access"
    con.execute("INSERT INTO tokens VALUES (?, 'access', 'claude-id', ?, ?)",
                (R._h(old), R._now() + 1800, R._now()))
    con.commit()
    con.close()
    new_con = R.db(path)
    assert R.valid_access(new_con, old)
    assert new_con.execute("SELECT resource FROM tokens WHERE hash=?", (R._h(old),)).fetchone()["resource"] == R.RESOURCE
    assert "resource" in {r["name"] for r in new_con.execute("PRAGMA table_info(codes)")}
    new_con.close()



def test_authorize_form_csp_allows_approved_oauth_callbacks(server):
    """Chrome enforces form-action on the POST -> cross-origin 302 callback too.

    If only 'self' is allowed, pairing succeeds (302) but Chrome blocks
    navigation to ChatGPT/Claude, leaving a stranded authorization code.
    """
    base, con = server
    _, _, reg = req(base + "/oauth/register", "POST", {
        "redirect_uris": [GPT_CB, CB], "client_name": "Moodle"})
    _, challenge = pkce()
    R.open_pairing(con)
    params = {"client_id": reg["client_id"], "redirect_uri": GPT_CB,
              "response_type": "code", "code_challenge_method": "S256",
              "code_challenge": challenge}
    st, headers, _ = req(base + "/oauth/authorize?" + urllib.parse.urlencode(params))
    assert st == 200
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "form-action 'self'" in csp
    for origin in ("https://chatgpt.com", "https://claude.ai", "https://claude.com"):
        assert origin in csp
    # The callback is still server-side allowlisted, not an arbitrary redirect.
    assert not R.redirect_allowed("https://evil.example/callback")


def test_remote_catalog_and_dispatch_never_open_login_or_sync(server):
    base, con = server
    _, tok = connect(base, con)
    auth = {"Authorization": "Bearer " + tok["access_token"]}
    _, _, reply = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, auth)
    tools = reply["result"]["tools"]
    assert len(tools) == 16 and all(t["annotations"]["readOnlyHint"] for t in tools)
    assert all(t["securitySchemes"] == [{"type": "oauth2", "scopes": ["monash"]}] for t in tools)
    for name in ("start_monash_login", "monash_login", "sync_monash_data", "monash_sync"):
        _, _, reply = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": name}}, auth)
        assert reply["error"]["code"] == -32602
    _, _, result = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "list_courses"}}, auth)
    assert not result["result"].get("isError")  # empty isolated course DB, no real credentials
    _, _, resource = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 4, "method": "resources/list"}, auth)
    assert resource["result"]["resources"][0]["uri"].startswith("ui://")
    st, _, fresh = req(base + "/oauth/token", "POST", form={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    assert st == 200
    assert req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 5, "method": "ping"},
               {"Authorization": "Bearer " + fresh["access_token"]})[2]["result"] == {}


def test_remote_rejects_bad_hosts_origins_bodies_and_batch(server):
    base, con = server
    assert req(base + "/oauth/register", "POST", data=[])[0] == 400
    assert req(base + "/.well-known/oauth-protected-resource", headers={"Host": "evil.example"})[0] == 403
    assert req(base + "/oauth/register", "POST", data={}, headers={"Origin": "https://evil.example"})[0] == 403
    _, tok = connect(base, con)
    auth = {"Authorization": "Bearer " + tok["access_token"]}
    assert req(base + "/mcp", "POST", data=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=auth)[0] == 400
    assert req(base + "/mcp", "POST", data={}, headers={**auth, "Content-Length": "-1"})[0] == 400
    assert req(base + "/mcp", "POST", data={}, headers={**auth, "Content-Length": str(R.MAX_BODY + 1)})[0] == 400
    for params in ([], {"name": {}}, {"name": "list_courses", "arguments": []}):
        _, _, reply = req(base + "/mcp", "POST", data={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params}, headers=auth)
        assert reply["error"]["code"] == -32602


def test_remote_token_database_permissions_and_hashed_storage(tmp_path):
    path = tmp_path / "private" / "remote.db"
    con = R.db(path)
    try:
        con.execute("INSERT INTO clients VALUES ('c','Test','[]',0)")
        token = R.issue_tokens(con, "c")
        hashes = {row[0] for row in con.execute("SELECT hash FROM tokens")}
        assert token["access_token"] not in hashes and token["refresh_token"] not in hashes
        assert R._h(token["access_token"]) in hashes
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        con.close()


def test_remote_requires_explicit_origin_and_loopback_binding(tmp_path, monkeypatch):
    for url in ("https://user:pass@example.org", "http://example.org", "https://example.org/path", "https://example.org?secret=x"):
        with pytest.raises(ValueError):
            R.configure(url)
    monkeypatch.delenv("MONASH_KIT_REMOTE_URL", raising=False)
    with pytest.raises(ValueError, match="public-url"):
        R.command(SimpleNamespace(action="serve", public_url=None))
    con = R.db(tmp_path / "test.db")
    try:
        with pytest.raises(ValueError, match="loopback"):
            R.make_server(("0.0.0.0", 0), con)
    finally:
        con.close()


def test_opt_in_servers_do_not_listen_when_imported(monkeypatch):
    import importlib
    import socket
    from monash_study_kit import library
    def no_bind(*args, **kwargs):
        raise AssertionError("Import must not start a server")
    monkeypatch.setattr(socket.socket, "bind", no_bind)
    importlib.reload(library)
    importlib.reload(R)


def test_extra_callback_is_opt_in_and_exact(monkeypatch):
    monkeypatch.setattr(R, "PUBLIC_URL", R.PUBLIC_URL)
    monkeypatch.setattr(R, "RESOURCE", R.RESOURCE)
    monkeypatch.setattr(R, "EXTRA_REDIRECTS", set())
    cb = "http://127.0.0.1:1455/auth/callback"
    assert not R.redirect_allowed(cb)
    R.configure("https://study.example.org", [cb])
    assert R.redirect_allowed(cb)
    assert not R.redirect_allowed("http://127.0.0.1:1456/auth/callback")
    for bad in ("http://example.org/callback", "https://example.org/*", "https://u:p@example.org/callback"):
        with pytest.raises(ValueError):
            R.configure("https://study.example.org", [bad])


# ---------------------------------------------------------------- 按客户端勾选邮件和考勤

def _student_settings(monkeypatch, **flags):
    from monash_study_kit import mcp_server
    monkeypatch.setattr(mcp_server, "load_settings", lambda: dict(flags))


def _authorize(base, con, extra_form):
    _, _, reg = req(base + "/oauth/register", "POST", {"redirect_uris": [CB], "client_name": "Claude"})
    cid = reg["client_id"]
    v, c = pkce()
    params = {"client_id": cid, "redirect_uri": CB, "response_type": "code",
              "code_challenge": c, "code_challenge_method": "S256"}
    _, _, page = req(base + "/oauth/authorize?" + urllib.parse.urlencode(params))
    st, h, _ = req(base + "/oauth/authorize", "POST",
                   form={**params, **extra_form, "pairing_code": R.open_pairing(con)})
    assert st == 302
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(h["Location"]).query))["code"]
    _, _, tok = req(base + "/oauth/token", "POST", form={"grant_type": "authorization_code", "code": code,
                                                         "redirect_uri": CB, "client_id": cid, "code_verifier": v})
    return page, tok


def _tools(base, tok):
    _, _, reply = req(base + "/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      {"Authorization": "Bearer " + tok["access_token"]})
    return {t["name"] for t in reply["result"]["tools"]}


def test_pairing_page_offers_only_features_the_server_turned_on(server, monkeypatch):
    base, con = server
    _student_settings(monkeypatch)
    page, tok = _authorize(base, con, {})
    assert "grant_" not in page and tok["scope"] == "monash"
    _student_settings(monkeypatch, gmail_enabled=True, gmail_manage_enabled=True)
    page, _ = _authorize(base, con, {})
    assert 'name="grant_gmail.read" value="1" checked' in page
    assert 'name="grant_gmail.manage" value="1">' in page        # 写操作默认不勾
    assert "grant_attendance" not in page


def test_client_chooses_features_and_refresh_keeps_the_choice(server, monkeypatch):
    base, con = server
    _student_settings(monkeypatch, gmail_enabled=True, attendance_enabled=True)
    # 表单里塞服务器没开的功能也没用
    _, tok = _authorize(base, con, {"grant_gmail.read": "1", "grant_attendance.submit": "1"})
    assert tok["scope"] == "monash gmail.read"
    names = _tools(base, tok)
    assert {"gmail_search", "gmail_inbox_todo", "list_moodle_due"} <= names and "attendance_status" not in names
    _, _, new = req(base + "/oauth/token", "POST", form={"grant_type": "refresh_token",
                                                           "refresh_token": tok["refresh_token"]})
    assert new["scope"] == "monash gmail.read" and "gmail_search" in _tools(base, new)
    # 另一个软件只勾考勤
    _, tok = _authorize(base, con, {"grant_attendance.read": "1"})
    names = _tools(base, tok)
    assert "attendance_overview" in names and "gmail_search" not in names


def test_clients_listing_shows_each_grant(server, monkeypatch, capsys, tmp_path):
    base, con = server
    _student_settings(monkeypatch, gmail_enabled=True)
    _authorize(base, con, {"grant_gmail.read": "1"})
    real_db = R.db
    monkeypatch.setattr(R, "db", lambda path=None: real_db(tmp_path / "r.db"))
    assert R.command(SimpleNamespace(action="clients")) == 0
    assert "Claude  monash gmail.read" in capsys.readouterr().out


def test_tokens_from_before_the_scopes_column_stay_moodle_and_ed_only(tmp_path):
    path = tmp_path / "legacy.db"
    con = R.sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE codes (hash TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT,
            challenge TEXT, expires INTEGER, resource TEXT NOT NULL DEFAULT '');
        CREATE TABLE tokens (hash TEXT PRIMARY KEY, kind TEXT, client_id TEXT,
            expires INTEGER, created INTEGER, resource TEXT NOT NULL DEFAULT '');
    """)
    con.execute("INSERT INTO tokens VALUES (?, 'access', 'old-client', ?, ?, '')",
                (R._h("old-access"), R._now() + 1800, R._now()))
    con.commit()
    con.close()
    new_con = R.db(path)
    assert R.access_grant(new_con, "old-access")["scopes"] == "monash"
    new_con.close()
