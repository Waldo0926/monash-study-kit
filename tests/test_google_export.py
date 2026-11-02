"""Google 导出只用合成元数据、假 HTTP 和假浏览器 cookie。"""
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from monash_study_kit import google_export as G


@pytest.mark.parametrize("url,kind,fid", [
    ("https://docs.google.com/presentation/d/abc/edit", "presentation", "abc"),
    ("https://docs.google.com/document/d/abc/edit", "document", "abc"),
    ("https://docs.google.com/spreadsheets/d/abc/edit", "spreadsheets", "abc"),
    ("https://drive.google.com/file/d/abc/view", "file", "abc"),
    ("https://drive.google.com/open?id=abc", "file", "abc"),
    ("https://docs.google.com/forms/d/e/abc/viewform", "forms", ""),
])
def test_parse_links(url, kind, fid):
    assert G.parse_link(url) == (kind, fid)


@pytest.mark.parametrize("url", ["https://docs.google.com.evil.example/document/d/a", "https://evil.example/open?id=a", "https://drive.google.com/open?id=../a", "javascript:alert(1)"])
def test_parse_rejects_unrelated_links(url):
    assert G.parse_link(url) is None


@pytest.mark.parametrize("status,reason,expected,fallback", [
    (403,"exportSizeLimitExceeded","超出大小限制",True),
    (403,"insufficientFilePermissions","没有权限",False),
    (403,"insufficientPermissions","没有权限",False),
    (403,"forbidden","没有权限",False),
    (403,"domainPolicy","管理员限制",True),
    (403,"cannotDownloadFile","管理员限制",True),
    (403,"cannotExportFile","管理员限制",True),
    (404,"","找不到或没共享给你",False),
    (401,"","需要重新授权",False),
    (400,"invalid_grant","需要重新授权",False),
])
def test_error_reasons(status,reason,expected,fallback):
    error = G.error_reason(status,reason)
    assert expected in str(error) and error.fallback == fallback


class Response(io.BytesIO):
    headers = {"Content-Type": "application/pdf"}


@pytest.fixture
def http(monkeypatch):
    seen = []
    answers = []
    def open(req, timeout):
        seen.append(req)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(G.urllib.request, "build_opener", lambda *a: SimpleNamespace(open=open))
    return seen, answers


@pytest.mark.parametrize("kind,mime,ext,endpoint", [
    ("presentation","application/vnd.google-apps.presentation",".pdf","/export?"),
    ("document","application/vnd.google-apps.document",".pdf","/export?"),
    ("spreadsheets","application/vnd.google-apps.spreadsheet",".xlsx","/export?"),
    ("file","application/pdf",".pdf","alt=media"),
])
def test_api_get_only_and_formats(http,kind,mime,ext,endpoint):
    seen, answers = http
    answers += [Response(json.dumps({"name":"Sample.pdf" if kind == "file" else "Sample", "mimeType":mime}).encode()), Response(b"file bytes")]
    url = f"https://{'drive' if kind == 'file' else 'docs'}.google.com/{kind}/d/abc"
    name, raw = G.download(url, G.Client(SimpleNamespace(valid=True, token="synthetic")))
    assert name.endswith(ext) and raw == b"file bytes"
    assert all(req.get_method() == "GET" for req in seen)
    assert "supportsAllDrives=true" in seen[0].full_url
    assert endpoint in seen[1].full_url
    assert "Authorization" not in seen[0].headers
    assert seen[0].unredirected_hdrs["Authorization"] == "Bearer synthetic"


def test_docx_setting(http,monkeypatch):
    monkeypatch.setattr(G,"load_settings",lambda: {"google_docs_format":"docx"})
    seen, answers = http
    answers += [Response(b'{"name":"Sample", "mimeType":"application/vnd.google-apps.document"}'), Response(b"zip")]
    assert G.download("https://docs.google.com/document/d/a", G.Client(SimpleNamespace(valid=True,token="fake")))[0].endswith(".docx")
    assert "wordprocessingml" in seen[1].full_url


@pytest.mark.parametrize("reason", ["exportSizeLimitExceeded", "domainPolicy"])
def test_size_and_admin_errors_use_browser(http,monkeypatch,reason):
    seen, answers = http
    answers += [Response(b'{"name":"Sample", "mimeType":"application/vnd.google-apps.presentation"}'),
                urllib.error.HTTPError(G.API,403,"Forbidden",{},io.BytesIO(json.dumps({"error":{"errors":[{"reason":reason}]}}).encode()))]
    calls=[]
    monkeypatch.setattr(G,"browser_export",lambda *args: calls.append(args) or b"browser pdf")
    assert G.download("https://docs.google.com/presentation/d/abc", G.Client(SimpleNamespace(valid=True,token="fake"))) == ("Sample.pdf",b"browser pdf")
    assert calls == [("presentation","abc","pdf")]


def test_browser_html_is_failure(http,monkeypatch):
    _, answers = http
    body=Response(b"<html>Sign in</html>")
    body.headers={"Content-Type":"text/html"}
    answers.append(body)
    monkeypatch.setattr(G,"browser_cookies",lambda: [])
    with pytest.raises(G.GoogleError,match="浏览器里没登录"):
        G.browser_export("presentation","abc","pdf")


@pytest.mark.parametrize("url",["https://evil.example/steal", "http://docs.google.com/a", "https://google.com.evil.example/a"])
def test_browser_redirect_stops_outside_google(url):
    handler=G._GoogleRedirect()
    with pytest.raises(G.GoogleError):
        handler.redirect_request(G.urllib.request.Request("https://docs.google.com/a"),None,302,"Found",{},url)


def test_browser_redirect_to_googleusercontent_is_allowed():
    handler=G._GoogleRedirect()
    req=handler.redirect_request(G.urllib.request.Request("https://docs.google.com/a"),None,302,"Found",{},
                                 "https://doc-0s-1c-docs.googleusercontent.com/docs/securesc/x")
    assert req.full_url.startswith("https://doc-0s-1c-docs.googleusercontent.com/")


def test_no_token_prompt(tmp_path,monkeypatch):
    monkeypatch.setattr(G,"TOKEN_FILE",tmp_path/"absent.json")
    with pytest.raises(G.GoogleError,match="还没授权 Google（monash google login）"):
        G.Client().get("files/a")


def test_forms_do_not_make_request():
    with pytest.raises(G.GoogleError,match=G.FORM_REASON):
        G.download("https://docs.google.com/forms/d/a",object())


def test_refresh_invalid_grant(tmp_path,monkeypatch):
    class Credentials:
        valid=False
        def refresh(self,request):
            raise RuntimeError("invalid_grant synthetic")
    monkeypatch.setattr(G,"_google",lambda: (lambda: object(),None,None))
    with pytest.raises(G.GoogleError,match="需要重新授权"):
        G.Client(Credentials()).get("files/a")


def test_logout_revokes_before_deleting(tmp_path,monkeypatch,http):
    token=tmp_path/"drive-token.json"
    token.write_text('{"refresh_token":"synthetic"}', encoding="utf-8")
    monkeypatch.setattr(G,"TOKEN_FILE",token)
    seen, answers=http
    class Revoked(Response):
        def read(self,*args):
            assert token.exists()
            return super().read(*args)
    answers.append(Revoked(b"ok"))
    assert G.logout() == {"已授权":False}
    assert not token.exists() and seen[0].get_method() == "POST" and seen[0].full_url == "https://oauth2.googleapis.com/revoke"


def test_logout_keeps_token_on_failure(tmp_path,monkeypatch,http):
    token=tmp_path/"drive-token.json"
    token.write_text('{"refresh_token":"synthetic"}', encoding="utf-8")
    monkeypatch.setattr(G,"TOKEN_FILE",token)
    http[1].append(OSError("network"))
    with pytest.raises(G.GoogleError,match="已保留"):
        G.logout()
    assert token.exists()


def test_browser_cookies_are_filtered_and_browser_closed(monkeypatch):
    from monash_study_kit import browser_login
    closed=[]
    cookies=[{"domain": d,"name":"SID","value":"fake"} for d in (".google.com","docs.google.com","evil.example","google.com.evil.example")]
    monkeypatch.setattr(browser_login,"Browser",lambda *a,**kw: SimpleNamespace(cdp=SimpleNamespace(call=lambda *a: {"cookies":cookies}),close=lambda: closed.append(True)))
    assert [c["domain"] for c in G.browser_cookies()] == [".google.com","docs.google.com"]
    assert closed == [True]


def test_login_only_drive_scope_and_separate_token(tmp_path,monkeypatch):
    client=tmp_path/"client.json"
    client.write_text('{"installed":{}}', encoding="utf-8")
    token=tmp_path/"drive-token.json"
    monkeypatch.setattr(G,"TOKEN_FILE",token)
    seen={}
    credentials=SimpleNamespace(refresh_token="fake",granted_scopes=[G.SCOPE],scopes=[G.SCOPE],to_json=lambda:'{"refresh_token":"fake","scopes":["'+G.SCOPE+'"]}')
    class Flow:
        @classmethod
        def from_client_config(cls, config, scopes):
            seen["scopes"]=scopes
            return cls()
        def run_local_server(self,**kw):
            seen.update(kw)
            return credentials
    monkeypatch.setattr(G,"_google",lambda:(None,None,Flow))
    G.login(client)
    assert seen["scopes"] == [G.SCOPE] and seen["host"] == "127.0.0.1" and seen["port"] == 0
    assert G.status()["已授权"] and G.TOKEN_FILE != G.gmail.TOKEN_FILE


def test_google_cli_dispatch(monkeypatch,capsys):
    from monash_study_kit import cli
    monkeypatch.setattr(G,"status",lambda:{"已授权":False,"scope":[]})
    args=cli.build_parser().parse_args(["google","status"])
    assert args.fn(args) == 0
    assert "已授权" in capsys.readouterr().out


def test_administrator_message_is_detected():
    error=G.error_reason(403,"unknown","Your administrator has disabled exports")
    assert str(error) == "管理员限制" and error.fallback


def test_disabled_drive_api_is_explained_and_tries_the_browser():
    e = G.error_reason(403, "accessNotConfigured", "Google Drive API has not been used in project 1 before or it is disabled.")
    assert "没启用 Drive API" in str(e) and e.fallback


def test_revoking_an_already_invalid_token_counts_as_revoked(tmp_path, monkeypatch):
    import io
    token = tmp_path / "google-drive-token.json"
    token.write_text('{"refresh_token":"synthetic"}', encoding="utf-8")
    def opener(*a, **k):
        class O:
            def open(self, req, timeout=None):
                raise G.urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_token"}'))
        return O()
    monkeypatch.setattr(G.urllib.request, "build_opener", opener)
    G.revoke_token(token)                      # 不报错
    def opener500(*a, **k):
        class O:
            def open(self, req, timeout=None):
                raise G.urllib.error.HTTPError(req.full_url, 500, "Error", {}, io.BytesIO(b"oops"))
        return O()
    monkeypatch.setattr(G.urllib.request, "build_opener", opener500)
    import pytest as _pytest
    with _pytest.raises(G.GoogleError):
        G.revoke_token(token)


def test_revoke_is_a_post_with_the_token_in_the_body(tmp_path, monkeypatch):
    token = tmp_path / "google-drive-token.json"
    token.write_text('{"refresh_token":"synthetic-refresh"}', encoding="utf-8")
    sent = []
    def opener(*a, **k):
        class O:
            def open(self, req, timeout=None):
                sent.append(req)
                class R:
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                    def read(self): return b""
                return R()
        return O()
    monkeypatch.setattr(G.urllib.request, "build_opener", opener)
    G.revoke_token(token)
    req = sent[0]
    assert req.get_method() == "POST" and req.full_url == "https://oauth2.googleapis.com/revoke"
    assert b"token=synthetic-refresh" in req.data and "synthetic" not in req.full_url
