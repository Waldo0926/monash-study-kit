"""安全边界：凭据只发给该发的地方、只存在自己能读的文件里、MCP 只读、读文件不越界。

docs/security.md 里每一条没有现成测试的说法，都在这里补一个。全部离线。
"""
import email.message
import io
import json
import os
import re
import sys
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from monash_study_kit import __version__, browser_login, cli, edlib, moodlelib, paths, update_check
from monash_study_kit import content_index as CI
from monash_study_kit import doctor, doctor_deep
from monash_study_kit import features as F
from monash_study_kit import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
PKG = Path(moodlelib.__file__).resolve().parent
SECRET_URL = "https://learning.monash.edu/calendar/export_execute.php?userid=1&authtoken=abc123def&preset_what=all"


# ---------------------------------------------------------------- Moodle cookie

class FakeResp:
    def __init__(self, status=200, location=None, body=b"", set_cookie=None):
        self.status = status
        self.headers = email.message.Message()
        if location:
            self.headers["Location"] = location
        if set_cookie:
            self.headers["Set-Cookie"] = set_cookie
        self._body = io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)

    def close(self):
        pass


def _fake_opener(monkeypatch, responses):
    sent = []

    def fake_open(req, timeout):
        sent.append(req)
        return responses.pop(0)
    monkeypatch.setattr(moodlelib, "REQUEST_GAP", 0)
    monkeypatch.setattr(moodlelib._opener, "open", fake_open)
    return sent


def _cookie_of(req):
    return req.get_header("Cookie") or req.unredirected_hdrs.get("Cookie")


def test_cookie_goes_to_moodle_but_not_to_the_cloudfront_hop(tmp_path, monkeypatch):
    sent = _fake_opener(monkeypatch, [
        FakeResp(302, location="https://d1.cloudfront.net/x/a.pdf?Signature=s"),
        FakeResp(200, body=b"%PDF-1.4", set_cookie="MoodleSession=stolen; path=/"),
    ])
    client = moodlelib.MoodleClient(jar={"MoodleSession": "real"}, persist=False)
    client.download("https://learning.monash.edu/pluginfile.php/1/a.pdf", tmp_path / "a.pdf")
    assert [urllib.parse.urlsplit(r.full_url).hostname for r in sent] == ["learning.monash.edu", "d1.cloudfront.net"]
    assert sent[0].unredirected_hdrs["Cookie"] == "MoodleSession=real"
    assert _cookie_of(sent[1]) is None
    assert client.jar == {"MoodleSession": "real"}          # 站外的 Set-Cookie 不收
    assert (tmp_path / "a.pdf").read_bytes() == b"%PDF-1.4"


def test_urllib_never_follows_redirects_by_itself():
    assert moodlelib._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None
    assert any(isinstance(h, moodlelib._NoRedirect) for h in moodlelib._opener.handlers)


def test_resolve_stops_at_the_first_offsite_hop(monkeypatch):
    sent = _fake_opener(monkeypatch, [
        FakeResp(303, location="/pluginfile.php/1/mod_resource/content/3/a.pdf"),
        FakeResp(302, location="https://docs.google.com/presentation/d/x"),
    ])
    client = moodlelib.MoodleClient(jar={"MoodleSession": "real"}, persist=False)
    local, external, _ = client.resolve("/mod/resource/view.php?id=1&redirect=1")
    assert external == "https://docs.google.com/presentation/d/x"
    assert local.endswith("/a.pdf") and len(sent) == 2       # 站外地址只记下来，不去请求


def test_okta_redirect_fails_before_it_is_followed(tmp_path, monkeypatch):
    sent = _fake_opener(monkeypatch, [FakeResp(302, location="https://monashuni.okta.com/app/sso/saml")])
    client = moodlelib.MoodleClient(jar={"MoodleSession": "old"}, persist=False)
    with pytest.raises(moodlelib.MoodleAuthError):
        client.download("https://learning.monash.edu/pluginfile.php/1/a.pdf", tmp_path / "a.pdf")
    assert len(sent) == 1 and not (tmp_path / "a.pdf").exists()


# ---------------------------------------------------------------- 登录窗口

def test_login_window_keeps_only_moodle_cookies():
    class FakeCDP:
        def call(self, method, params=None, timeout=15):
            return {"cookies": [
                {"name": "MoodleSession", "value": "good", "domain": "learning.monash.edu"},
                {"name": "MOODLEID1_", "value": "id", "domain": ".monash.edu"},
                {"name": "MoodleSession", "value": "phish", "domain": "learning.monash.edu.evil.com"},
                {"name": "AWSALB", "value": "x", "domain": "ash.edu"},
                {"name": "sid", "value": "okta", "domain": "monashuni.okta.com"},
                {"name": "_ga", "value": "track", "domain": ".monash.edu"},
            ]}
    b = object.__new__(browser_login.Browser)
    b.cdp = FakeCDP()
    assert b.cookies() == {"MoodleSession": "good", "MOODLEID1_": "id"}


def test_login_window_uses_its_own_private_profile(tmp_path, monkeypatch):
    profile = tmp_path / "browser-profile"
    seen = {}
    monkeypatch.setattr(browser_login, "BROWSER_PROFILE", profile)
    monkeypatch.setattr(browser_login, "find_browser", lambda: "/usr/bin/chrome")
    if os.name != "nt":
        class FakePipe:
            def __init__(self, argv):
                seen["args"] = argv
                self.proc = None

            def call(self, method, params=None, timeout=15):
                return {"product": "Chrome/fake"}
        monkeypatch.setattr(browser_login, "PipeCDP", FakePipe)
    else:
        class FakePopen:
            def __init__(self, args, **kw):
                seen["args"] = args
                (profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/x\n", encoding="utf-8")

            def poll(self):
                return None
        monkeypatch.setattr(browser_login.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(browser_login, "CDP", lambda url: seen.setdefault("ws", url))
    browser_login.Browser("https://learning.monash.edu/my/")
    assert f"--user-data-dir={profile}" in seen["args"]
    if os.name != "nt":
        # macOS / Linux：DevTools 走管道，不开端口，本机别的程序连不上登录窗口
        assert "--remote-debugging-pipe" in seen["args"]
        assert not any(a.startswith("--remote-debugging-port") for a in seen["args"])
        assert profile.stat().st_mode & 0o777 == 0o700
    else:
        assert seen["ws"].startswith("ws://127.0.0.1:")


def _real_chromium():
    import glob
    import shutil
    found = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
    return found[0] if found else shutil.which("chromium") or shutil.which("google-chrome")


@pytest.mark.skipif(os.name == "nt" or not _real_chromium(), reason="要一个真的 Chromium")
def test_pipe_cdp_talks_to_a_real_browser_without_opening_a_port(tmp_path):
    from monash_study_kit.cdp import PipeCDP
    # CI 上的 google-chrome 偶尔启动很慢（第一次运行、找钥匙串），30 秒不够，多给点时间，能跳过的都跳过
    c = PipeCDP([_real_chromium(), "--headless=new", "--no-sandbox", f"--user-data-dir={tmp_path}",
                 "--no-first-run", "--no-default-browser-check", "--password-store=basic",
                 "--remote-debugging-pipe", "about:blank"])
    try:
        assert c.call("Browser.getVersion", timeout=90)["product"].startswith(("Chrome", "HeadlessChrome"))
        assert c.call("Storage.getCookies") == {"cookies": []}
        assert not (tmp_path / "DevToolsActivePort").exists()      # 开端口的话 Chrome 会写这个文件
    finally:
        try:
            c.call("Browser.close", timeout=5)
        except Exception:  # noqa: BLE001
            pass
        c.proc.wait(timeout=10)
        c.close()


# ---------------------------------------------------------------- 凭据落盘

@pytest.mark.skipif(os.name == "nt", reason="Windows 上 chmod 不管用")
def test_write_secret_is_owner_only_and_atomic(tmp_path, monkeypatch):
    target = tmp_path / "secrets" / "ed-token"
    target.parent.mkdir()
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o644)                                  # 以前留下的、权限太宽的文件
    renamed = []
    real_replace = os.replace
    monkeypatch.setattr(paths.os, "replace", lambda a, b: renamed.append(Path(a).name) or real_replace(a, b))
    paths.write_secret(target, "new")
    assert renamed == [f"ed-token.{os.getpid()}.tmp"]       # 临时文件名带进程号
    assert target.read_text(encoding="utf-8") == "new"
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in target.parent.iterdir()] == ["ed-token"]


def test_all_three_secrets_live_in_the_secrets_dir():
    assert moodlelib.COOKIE_FILE == paths.SECRETS_DIR / "moodle-cookie"
    assert F.CALENDAR_URL_FILE == paths.SECRETS_DIR / "calendar-url"
    if not os.environ.get("MONASH_KIT_ED_TOKEN"):
        assert edlib.TOKEN_FILE == paths.SECRETS_DIR / "ed-token"


# ---------------------------------------------------------------- Ed 令牌

def test_no_tool_takes_a_credential_argument():
    for tool in M.tool_list():
        for prop in tool["inputSchema"]["properties"]:
            assert not re.search(r"token|cookie|password|secret|session", prop, re.I), (tool["name"], prop)
    assert "令牌别贴进聊天" in M.INSTRUCTIONS


def test_ed_token_is_read_with_getpass_in_the_terminal(monkeypatch):
    saved = []

    class FakeEd:
        def __init__(self, token):
            self.token = token

        def me(self):
            return {"user": {"name": "A", "email": "a@student.monash.edu"}}
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "abc.def\n")
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: None)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("令牌不能用会回显的 input() 读"))
    monkeypatch.setattr(edlib, "EdClient", FakeEd)
    monkeypatch.setattr(edlib, "save_token", saved.append)
    assert cli.login_ed() is True and saved == ["abc.def"]


def test_ed_requests_only_go_to_the_ed_api(monkeypatch):
    urls = []

    def fake_urlopen(req, timeout):
        urls.append((req.full_url, req.get_method()))
        return io.BytesIO(b'{"user": {}, "token": "a.b.c"}')
    monkeypatch.setattr(edlib.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(edlib, "save_token", lambda t: None)
    c = edlib.EdClient(token="h.p.s", delay=0)
    c.me()
    c.get("/courses/1/threads", {"limit": 1})
    c.renew()
    assert {urllib.parse.urlsplit(u).netloc for u, _ in urls} == {"edstem.org"}
    assert all(u.startswith("https://edstem.org/api/") for u, _ in urls)
    assert [m for _, m in urls] == ["GET", "GET", "POST"]


# ---------------------------------------------------------------- MCP：stdio、只读

def test_mcp_talks_over_stdin_and_stdout(monkeypatch):
    out = io.BytesIO()
    stdout = io.TextIOWrapper(out, encoding="utf-8")
    stdin = io.TextIOWrapper(io.BytesIO(b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n'), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    M.main(background=False)
    stdout.flush()
    assert json.loads(out.getvalue()) == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_only_explicit_opt_in_modules_can_listen():
    for f in PKG.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        if f.name not in {"library.py", "remote.py"}:
            assert not re.search(r"http\.server|socketserver|\.listen\(|\.bind\(|asyncio\.start_server", src), f.name
        if re.search(r"^import socket|^from socket", src, re.M):
            assert f.name == "cdp.py"                        # 只有 CDP 客户端去连本机的 DevTools


def test_only_read_calls_reach_moodle_and_ed():
    src = {f.name: f.read_text(encoding="utf-8") for f in PKG.glob("*.py")}
    ajax = {m for s in src.values() for m in re.findall(r'\.call\(\s*"([a-z_]+)"', s)}
    assert ajax and all("_get_" in m or m in ("core_session_touch", "core_session_time_remaining") for m in ajax), ajax
    posts = {p for s in src.values() for p in re.findall(r'\.post\(\s*f?"([^"]+)"', s)}
    assert posts == {"/calendar/export.php", "/renew_token"}
    calls = sum(len(re.findall(r"(?<!def )\bmultipart\(", s)) for s in src.values())
    assert calls == 0                                        # 上传用的编码器留着，但没有人调用


# ---------------------------------------------------------------- read_file 不越界

@pytest.fixture
def two_dirs(tmp_path, monkeypatch):
    files, ed = tmp_path / "files", tmp_path / "ed"
    (files / "U").mkdir(parents=True)
    (ed / "1").mkdir(parents=True)
    (files / "U" / "a.md").write_text("ok", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    monkeypatch.setattr(CI, "FILES_DIR", files)
    monkeypatch.setattr(CI, "ED_FILES_DIR", ed)
    return tmp_path


@pytest.mark.parametrize("bad", ["../id_rsa", "U/../../id_rsa", "ed:1/../../id_rsa", "U", "", "ed:"])
def test_resolve_readable_rejects_paths_outside_the_file_dirs(two_dirs, bad):
    with pytest.raises(ValueError):
        CI.resolve_readable(bad)


def test_resolve_readable_turns_absolute_paths_into_relative_ones(two_dirs):
    with pytest.raises(ValueError):
        CI.resolve_readable(str(two_dirs / "id_rsa"))        # 前面的 / 被去掉，落在课件目录里，找不到
    assert CI.resolve_readable("/U/a.md") == (two_dirs / "files" / "U" / "a.md").resolve()


def test_resolve_readable_rejects_a_symlink_that_points_outside(two_dirs):
    link = two_dirs / "files" / "U" / "notes.md"
    try:
        link.symlink_to(two_dirs / "id_rsa")
    except (OSError, NotImplementedError):
        pytest.skip("这个系统建不了符号链接")
    with pytest.raises(ValueError):
        CI.resolve_readable("U/notes.md")


# ---------------------------------------------------------------- 索引的大小上限

def test_zip_indexing_reads_at_most_zip_members_max(tmp_path):
    z = tmp_path / "many.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for i in range(CI.ZIP_MEMBERS_MAX + 5):
            zf.writestr(f"f{i:03d}.txt", f"word{i}")
    assert len({loc for loc, _ in CI.zip_chunks(z)}) == CI.ZIP_MEMBERS_MAX


def test_files_over_max_file_are_not_indexed(tmp_path, monkeypatch):
    files = tmp_path / "files" / "FIT1000 X"
    files.mkdir(parents=True)
    (files / "big.md").write_text("huge " * 100, encoding="utf-8")
    (files / "small.md").write_text("tiny", encoding="utf-8")
    monkeypatch.setattr(CI, "FILES_DIR", tmp_path / "files")
    monkeypatch.setattr(CI, "ED_FILES_DIR", tmp_path / "no-ed")
    monkeypatch.setattr(CI, "MAX_FILE", 100)
    import sqlite3
    con = sqlite3.connect(":memory:")
    assert CI.index(con)["indexed"] == 1
    assert CI.search(con, "huge") == []


# ---------------------------------------------------------------- doctor 输出

def test_scrub_also_hides_ed_token_headers():
    out = doctor.scrub("x-token: eyJhbGc.eyJleHA.sig failed; token=abc123")
    assert "eyJhbGc" not in out and "abc123" not in out


def test_doctor_deep_errors_are_scrubbed(monkeypatch):
    def boom(ctx):
        raise RuntimeError(f"GET {SECRET_URL} -> 500")
    boom.__name__ = "deep_due"
    monkeypatch.setattr(doctor_deep, "DEEP_CHECKS", [boom])
    [r] = doctor_deep.run()
    assert r["status"] == doctor.WARN and "abc123def" not in r["detail"] and "authtoken=…" in r["detail"]


def test_calendar_url_is_masked_in_cli_and_absent_from_mcp(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(F, "load_calendar_url", lambda: SECRET_URL)
    cli.cmd_m_calendar(argparse.Namespace(refresh=False, show=False, json=True))
    printed = capsys.readouterr().out
    assert "abc123def" not in printed and "authtoken=…" in printed
    status = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "get_monash_status", "arguments": {}}})["result"]
    assert "abc123def" not in status["content"][0]["text"]


# ---------------------------------------------------------------- 新版本检查

def test_update_check_sends_only_a_versioned_user_agent(monkeypatch):
    reqs = []

    def fake_urlopen(req, timeout):
        reqs.append(req)
        return io.BytesIO(b'{"info": {"version": "9.9.9"}}')
    monkeypatch.setattr(update_check.urllib.request, "urlopen", fake_urlopen)
    assert update_check.fetch_latest() == "9.9.9"
    [req] = reqs
    assert req.header_items() == [("User-agent", f"monash-study-kit/{__version__}")]
    assert req.data is None and urllib.parse.urlsplit(req.full_url).hostname == "pypi.org"
    assert urllib.parse.urlsplit(update_check.VERSION_URL).hostname == "raw.githubusercontent.com"


# ---------------------------------------------------------------- 供应链

def test_one_runtime_dependency_and_heavy_media_is_optional():
    # 核心只依赖 pypdf；录播、Gmail、考勤要用才装，不能悄悄变成必装
    tomllib = pytest.importorskip("tomllib")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == ["pypdf>=4.0"]
    assert set(project["optional-dependencies"]) == {"media", "gmail", "attendance"}


def test_release_uses_trusted_publishing_without_stored_secrets():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "id-token: write" in wf and "pypa/gh-action-pypi-publish" in wf
    assert "secrets." not in wf and "password" not in wf.lower()
    assert 'if [ "$tag" != "$version" ]' in wf               # tag 和 __version__ 对不上就不发


# ---------------------------------------------------------------- 安全检查发现后补上的

def test_cookie_is_not_sent_over_plain_http(monkeypatch):
    sent = _fake_opener(monkeypatch, [FakeResp(200, body=b"ok"), FakeResp(200, body=b"ok")])
    client = moodlelib.MoodleClient(jar={"MoodleSession": "real"}, persist=False)
    client.get("http://learning.monash.edu/course/view.php?id=1")
    client.get("https://learning.monash.edu/course/view.php?id=1")
    assert _cookie_of(sent[0]) is None and _cookie_of(sent[1]) == "MoodleSession=real"


def test_ed_token_is_not_carried_across_a_redirect():
    import urllib.request
    req = edlib.EdClient(token="h.p.s", delay=0)._request("https://edstem.org/api/user")
    assert "x-token" not in {k.lower() for k in req.headers} and "Authorization" not in req.headers
    moved = urllib.request.HTTPRedirectHandler().redirect_request(
        req, None, 302, "Found", {}, "https://evil.example/steal")
    assert not any("h.p.s" in v for v in moved.headers.values())
    assert not any("h.p.s" in v for v in moved.unredirected_hdrs.values())


def test_read_file_refuses_a_zip_member_that_inflates_too_far(two_dirs, monkeypatch):
    monkeypatch.setattr(CI, "MAX_FILE", 1000)
    with zipfile.ZipFile(two_dirs / "files" / "U" / "bomb.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.txt", "0" * 50_000)                    # 压缩后很小，解开 50 KB
        z.writestr("small.txt", "hello")
    r = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "read_file", "arguments": {"path": "U/bomb.zip", "inner": "big.txt"}}})["result"]
    assert r["isError"] and "太大" in r["content"][0]["text"]
    r = M.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "read_file", "arguments": {"path": "U/bomb.zip", "inner": "small.txt"}}})["result"]
    assert "hello" in r["content"][0]["text"]


def test_read_file_refuses_huge_text_and_office_xml(two_dirs, monkeypatch):
    from monash_study_kit import textextract
    monkeypatch.setattr(CI, "MAX_FILE", 1000)
    (two_dirs / "files" / "U" / "log.txt").write_text("x" * 5000, encoding="utf-8")
    r = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "read_file", "arguments": {"path": "U/log.txt"}}})["result"]
    assert r["isError"]
    monkeypatch.setattr(textextract, "OFFICE_XML_MAX", 1000)
    with zipfile.ZipFile(two_dirs / "files" / "U" / "a.docx", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:p>" + "a" * 5000 + "</w:p>")
    with pytest.raises(ValueError):
        textextract.office_parts(two_dirs / "files" / "U" / "a.docx")


def test_doctor_scrubs_sesskey_and_crashed_checks(monkeypatch):
    assert "s3cr3t" not in doctor.scrub("POST /lib/ajax/service.php?sesskey=s3cr3t&info=x")

    def crash():
        raise RuntimeError("GET https://x/export_execute.php?authtoken=abc123 failed")
    monkeypatch.setattr(doctor, "CHECKS", [crash])
    assert "abc123" not in json.dumps(doctor.run(), ensure_ascii=False)


def test_index_skips_symlinks_that_point_outside(two_dirs):
    link = two_dirs / "files" / "U" / "notes.md"
    try:
        link.symlink_to(two_dirs / "id_rsa")
    except (OSError, NotImplementedError):
        pytest.skip("这个系统建不了符号链接")
    inside = two_dirs / "files" / "U" / "alias.md"
    inside.symlink_to(two_dirs / "files" / "U" / "a.md")      # 指向目录里面的软链接照常索引
    names = {Path(f["path"]).name for f in CI.candidates()}
    assert "notes.md" not in names and {"a.md", "alias.md"} <= names


def test_set_cookie_over_plain_http_is_ignored(monkeypatch):
    _fake_opener(monkeypatch, [FakeResp(200, body=b"ok", set_cookie="MoodleSession=injected; path=/")])
    client = moodlelib.MoodleClient(jar={"MoodleSession": "real"}, persist=False)
    client.get("http://learning.monash.edu/course/view.php?id=1")
    assert client.jar == {"MoodleSession": "real"}

