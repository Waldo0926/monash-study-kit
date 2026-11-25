"""OnTrack / Doubtfire 集成测试：全部离线，不触及任何真实站点。"""
from __future__ import annotations

import email.message
import io
import json
import urllib.error
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest

from monash_study_kit import cli, doctor, help_menu, ontrack, remote, semester, student_tools, todo
from monash_study_kit import mcp_server as M


def _future_iso(minutes: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _past_iso(minutes: int = 30) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _sample_project() -> dict:
    return {
        "id": 42,
        "unit": {
            "id": 7,
            "code": "FIT2004",
            "name": "Algorithms and Data Structures",
            "active": True,
            "allow_flexible_dates": True,
        },
        "target_grade": 2,
        "submitted_grade": None,
        "compile_portfolio": False,
        "portfolio_available": True,
        "uses_draft_learning_summary": False,
        "spec_con_days": 2,
        "tasks": [
            {
                "id": 101,
                "task_definition_id": 11,
                "status": "working_on_it",
                "due_date": "2026-10-10",
                "target_due_date": None,
                "target_start_date": None,
                "submission_date": None,
                "completion_date": None,
                "moved_to_discuss_at": None,
                "discuss_timeout_expiry_at": None,
                "extensions": -1,
                "times_assessed": 0,
                "grade": None,
                "quality_pts": None,
                "include_in_portfolio": True,
                "num_new_comments": 2,
            },
            {
                "id": 102,
                "task_definition_id": 12,
                "status": "complete",
                "due_date": "2026-10-01",
                "target_due_date": "2026-10-01",
                "target_start_date": None,
                "submission_date": "2026-09-30",
                "completion_date": "2026-10-01",
                "moved_to_discuss_at": None,
                "discuss_timeout_expiry_at": None,
                "extensions": 0,
                "times_assessed": 1,
                "grade": 2,
                "quality_pts": None,
                "include_in_portfolio": True,
                "num_new_comments": 0,
            },
            {
                "id": 103,
                "task_definition_id": 13,
                "status": "fix_and_resubmit",
                "due_date": "2026-10-05",
                "target_due_date": None,
                "target_start_date": None,
                "submission_date": "2026-10-04",
                "completion_date": None,
                "moved_to_discuss_at": "2026-10-06T00:00:00Z",
                "discuss_timeout_expiry_at": "2026-10-07T00:00:00Z",
                "extensions": 0,
                "times_assessed": 1,
                "grade": None,
                "quality_pts": None,
                "include_in_portfolio": True,
                "num_new_comments": 0,
            },
        ],
    }


def _sample_unit() -> dict:
    return {
        "id": 7,
        "code": "FIT2004",
        "name": "Algorithms and Data Structures",
        "active": True,
        "allow_flexible_dates": True,
        "description": "Core algorithms",
        "grade_definitions": [
            {"id": "p", "value": 0, "name": "Pass", "abbreviation": "P"},
            {"id": "c", "value": 1, "name": "Credit", "abbreviation": "C"},
            {"id": "d", "value": 2, "name": "Distinction", "abbreviation": "D"},
            {"id": "hd", "value": 3, "name": "High Distinction", "abbreviation": "HD"},
        ],
        "task_definitions": [
            {
                "id": 11,
                "abbreviation": "1.1P",
                "name": "Graph Search",
                "description": "Implement BFS and DFS",
                "target_grade": 0,
                "start_date": "2026-10-08",
                "target_date": "2026-10-12",
                "due_date": "2026-10-12",
                "is_graded": False,
                "max_quality_pts": 0,
                "has_task_sheet": True,
                "has_task_resources": True,
                "upload_requirements": [
                    {"key": "file0", "name": "Report PDF", "type": "document", "submission_history": False},
                ],
                "grade_due_dates": [
                    {"target_grade": 2, "target_due_date": "2026-10-14", "start_date": "2026-10-06"},
                ],
            },
            {
                "id": 12,
                "abbreviation": "0.1P",
                "name": "Setup",
                "description": "Environment check",
                "target_grade": 0,
                "start_date": "2026-09-25",
                "target_date": "2026-10-01",
                "due_date": "2026-10-01",
                "is_graded": False,
                "max_quality_pts": 0,
                "has_task_sheet": False,
                "has_task_resources": False,
                "upload_requirements": [],
                "grade_due_dates": [],
            },
            {
                "id": 13,
                "abbreviation": "2.1C",
                "name": "Shortest Paths",
                "description": "Dijkstra",
                "target_grade": 1,
                "start_date": "2026-10-01",
                "target_date": "2026-10-05",
                "due_date": "2026-10-05",
                "is_graded": True,
                "max_quality_pts": 5,
                "has_task_sheet": True,
                "has_task_resources": False,
                "upload_requirements": [],
                "grade_due_dates": [],
            },
        ],
    }


def _sample_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello ontrack")
    return buf.getvalue()


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = io.BytesIO(body)
        self.headers = email.message.Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def read(self, n=-1):
        return self._body.read(n)

    def close(self):
        pass


@pytest.fixture
def isolated_ontrack(tmp_path, monkeypatch):
    monkeypatch.setattr(ontrack, "SESSION_FILE", tmp_path / "secrets" / "ontrack-session.json")
    monkeypatch.setattr(ontrack, "CACHE_DIR", tmp_path / "data" / "ontrack")
    monkeypatch.setattr(ontrack, "CACHE_FILE", tmp_path / "data" / "ontrack" / "snapshot.json")
    monkeypatch.setattr(ontrack, "SHEETS_DIR", tmp_path / "data" / "ontrack" / "sheets")
    return tmp_path


# ---------------------------------------------------------------- URL、安全边界与脱敏

def test_normalize_base_url_enforces_https_and_rejects_credentials(monkeypatch):
    assert ontrack.normalize_base_url("https://ontrack.example.edu/") == "https://ontrack.example.edu"
    assert ontrack.normalize_base_url("ontrack.example.edu/app/") == "https://ontrack.example.edu/app"
    assert ontrack.normalize_base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    monkeypatch.setattr(ontrack, "load_settings", lambda: {})
    assert ontrack.configured_base_url() == "https://ontrack.infotech.monash.edu"
    assert ontrack.configured_base_url("https://other.example.edu/") == "https://other.example.edu"
    for bad in (
        "http://ontrack.example.edu",
        "https://user:pass@ontrack.example.edu",
        "https://ontrack.example.edu?a=1",
        "https://ontrack.example.edu#frag",
        "",
    ):
        with pytest.raises(ValueError):
            ontrack.normalize_base_url(bad)


def test_http_never_follows_redirects_and_redacts_token(monkeypatch):
    assert ontrack._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None
    assert any(isinstance(h, ontrack._NoRedirect) for h in ontrack._opener.handlers)

    sent = []

    def fake_open(req, timeout):
        sent.append(req)
        raise urllib.error.HTTPError(
            req.full_url, 500, "Server Error", email.message.Message(),
            io.BytesIO(b'{"error":"bad secret-tok-999"}'),
        )

    monkeypatch.setattr(ontrack._opener, "open", fake_open)
    client = ontrack.OnTrackClient(
        session={
            "base_url": "https://ontrack.example.edu",
            "username": "alice",
            "access_token": "secret-tok-999",
            "provenance": "environment",
        },
        delay=0,
    )
    with pytest.raises(ontrack.OnTrackError) as exc:
        client.projects()
    assert "secret-tok-999" not in str(exc.value)
    assert "[redacted]" in str(exc.value)
    assert sent[0].unredirected_hdrs["Auth-token"] == "secret-tok-999"
    assert "Auth-token" not in sent[0].headers


def test_filter_applicable_cookies_enforces_domain_path_and_expiry():
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    records = [
        {"name": "refresh_token", "value": "good", "domain": ".example.edu", "path": "/", "secure": True},
        {"name": "username", "value": "alice", "domain": "ontrack.example.edu", "path": "/api/auth"},
        {"name": "refresh_token", "value": "spoof", "domain": "ontrack.example.edu.evil.com", "path": "/"},
        {"name": "refresh_token", "value": "suffix-spoof", "domain": "ple.edu", "path": "/"},
        {"name": "refresh_token", "value": "expired", "domain": "ontrack.example.edu", "expires": 1000},
        {"name": "refresh_token", "value": "bad;inject", "domain": "ontrack.example.edu"},
        {"name": "refresh_token", "value": "wrong-path", "domain": "ontrack.example.edu", "path": "/other"},
    ]
    got = ontrack.filter_applicable_cookies(records, "https://ontrack.example.edu", now)
    assert [(c["name"], c["value"]) for c in got] == [("refresh_token", "good"), ("username", "alice")]


# ---------------------------------------------------------------- 认证与会话续期

def test_parse_pasted_credentials_supports_json_curl_cookie_and_bare():
    base = "https://ontrack.example.edu"
    raw_json = json.dumps({
        "auth_token": "tok1",
        "auth_token_expiry": _future_iso(15),
        "user": {"username": "alice", "first_name": "Alice"},
    })
    parsed = ontrack.parse_pasted_credentials(raw_json, base)
    assert parsed["kind"] == "session" and parsed["session"]["username"] == "alice"

    curl = "curl 'https://ontrack.example.edu/api/projects' -H 'Cookie: username=alice; refresh_token=r123'"
    parsed = ontrack.parse_pasted_credentials(curl, base)
    assert parsed["kind"] == "cookies"
    assert {c["name"]: c["value"] for c in parsed["cookies"]} == {"username": "alice", "refresh_token": "r123"}

    bare = ontrack.parse_pasted_credentials("r999", base, username="bob")
    assert {c["name"]: c["value"] for c in bare["cookies"]} == {"username": "bob", "refresh_token": "r999"}


def test_resolve_session_renews_via_refresh_cookie_and_strips_dead_cookie(isolated_ontrack, monkeypatch):
    base = "https://ontrack.example.edu"
    monkeypatch.setattr(ontrack, "load_settings", lambda: {"ontrack_url": base, "auto_relogin": False})
    ontrack.save_stored_session({
        "base_url": base,
        "username": "alice",
        "access_token": "old-tok",
        "auth_token_expiry": _past_iso(5),
        "provenance": "browser",
        "refresh_cookies": [
            {"name": "username", "value": "alice", "domain": "ontrack.example.edu", "path": "/"},
            {"name": "refresh_token", "value": "rt-good", "domain": "ontrack.example.edu", "path": "/"},
        ],
    })

    def fake_exchange(url, cookies, **kw):
        assert any(c["value"] == "rt-good" for c in cookies)
        return {
            "base_url": base,
            "username": "alice",
            "access_token": "fresh-tok",
            "auth_token_expiry": _future_iso(15),
            "provenance": "browser",
            "user": {"username": "alice"},
            "refresh_cookies": cookies,
        }

    monkeypatch.setattr(ontrack, "exchange_refresh_cookies", fake_exchange)
    sess = ontrack.resolve_session()
    assert sess["access_token"] == "fresh-tok"
    assert ontrack.load_stored_session(base)["access_token"] == "fresh-tok"

    # 若服务端明确拒绝 refresh cookie，则清掉失效的 refresh_cookies 并报 OnTrackAuthError
    monkeypatch.setattr(ontrack, "exchange_refresh_cookies", lambda *a, **kw: None)
    with pytest.raises(ontrack.OnTrackAuthError):
        ontrack.resolve_session(force_refresh=True)
    stored = ontrack.load_stored_session(base)
    assert "refresh_cookies" not in stored


# ---------------------------------------------------------------- 快照、截止与下载校验

def test_build_project_snapshot_flexible_dates_extensions_and_sorting():
    proj = ontrack.parse_project(_sample_project())
    unit = ontrack.parse_unit(_sample_unit())
    snap = ontrack.build_project_snapshot(
        proj,
        unit,
        today=date(2026, 10, 10),
        now=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
    )
    tasks = {t["abbreviation"]: t for t in snap["tasks"]}
    # 顺序：0.1P (10-01) -> 2.1C (10-05) -> 1.1P (flexible grade D = 10-14)
    assert [t["abbreviation"] for t in snap["tasks"]] == ["0.1P", "2.1C", "1.1P"]
    # 1.1P: target_grade=2 -> grade_due_date 2026-10-14, spec_con_days=2 -> deadline 2026-10-16
    assert tasks["1.1P"]["due_date"] == "2026-10-14"
    assert tasks["1.1P"]["deadline"] == "2026-10-16"
    assert tasks["1.1P"]["is_overdue"] is False
    assert tasks["1.1P"]["unread_comments"] == 2
    # 0.1P: complete 即使在过去也不算逾期
    assert tasks["0.1P"]["is_overdue"] is False
    assert tasks["0.1P"]["grade_label"] == "D (Distinction)"
    # 2.1C: due 10-05 + spec_con 2 = 10-07 < 10-10 -> overdue 且 discuss_timeout 已过
    assert tasks["2.1C"]["deadline"] == "2026-10-07"
    assert tasks["2.1C"]["is_overdue"] is True
    assert tasks["2.1C"]["is_discuss_overdue"] is True


def test_download_file_reassembles_206_range_and_validates_zip_and_pdf(monkeypatch):
    zip_bytes = _sample_zip_bytes()
    split = len(zip_bytes) // 2
    responses = [
        FakeResp(206, zip_bytes[:split], {
            "Content-Range": f"bytes 0-{split - 1}/{len(zip_bytes)}",
            "Content-Type": "application/zip",
            "Content-Disposition": 'attachment; filename="FIT2004.zip"',
        }),
        FakeResp(206, zip_bytes[split:], {
            "Content-Range": f"bytes {split}-{len(zip_bytes) - 1}/{len(zip_bytes)}",
            "Content-Type": "application/zip",
        }),
    ]
    monkeypatch.setattr(ontrack._opener, "open", lambda req, timeout: responses.pop(0))
    client = ontrack.OnTrackClient(
        session={"base_url": "https://ontrack.example.edu", "username": "alice", "access_token": "tok", "provenance": "environment"},
        delay=0,
    )
    dl = client.download_file("api/units/7/all_resources")
    assert dl["bytes"] == zip_bytes
    assert dl["filename"] == "FIT2004.zip"
    assert ontrack.is_valid_zip(dl["bytes"]) is True
    assert ontrack.is_valid_zip(b"<html>not a zip</html>") is False

    # filenotfound.pdf 被拦截
    monkeypatch.setattr(
        ontrack._opener,
        "open",
        lambda req, timeout: FakeResp(200, b"%PDF-1.4\n", {
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="FileNotFound.pdf"',
        }),
    )
    with pytest.raises(ontrack.OnTrackError, match="没有任务书"):
        client.download_task_sheet(7, 11)


def test_prepare_uploads_rejects_symlinks_empty_and_count_mismatch(tmp_path):
    reqs = [{"key": "file0", "name": "Report", "type": "document", "submission_history": False}]
    good = tmp_path / "report.pdf"
    good.write_bytes(b"%PDF-1.4 hello")
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")

    with pytest.raises(ValueError, match="需要按要求顺序提交 1 个文件"):
        ontrack.prepare_uploads([], reqs)
    with pytest.raises(ValueError, match="为空"):
        ontrack.prepare_uploads([str(empty)], reqs)

    link = tmp_path / "link.pdf"
    try:
        link.symlink_to(good)
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(ValueError, match="符号链接"):
            ontrack.prepare_uploads([str(link)], reqs)

    uploads = ontrack.prepare_uploads([str(good)], reqs)
    assert len(uploads) == 1 and uploads[0]["key"] == "file0" and uploads[0]["content_type"] == "application/pdf"
    ctype, body = ontrack._encode_form_data([("trigger", "ready_for_feedback")], [
        (uploads[0]["key"], uploads[0]["filename"], uploads[0]["content_type"], uploads[0]["bytes"]),
    ])
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="file0"; filename="report.pdf"' in body and b"%PDF-1.4 hello" in body


# ---------------------------------------------------------------- 同步、todo、semester、CLI

class FakeClient:
    base_url = "https://ontrack.example.edu"
    session = {"base_url": base_url, "username": "alice", "access_token": "tok", "user": {"username": "alice", "first_name": "Alice"}}
    writes = []

    def auth_method(self):
        return {"method": "saml", "redirect_to": "https://sso.example.edu/saml"}

    def projects(self, include_inactive=False):
        return [ontrack.parse_projects([{
            "id": 42,
            "unit": {"id": 7, "code": "FIT2004", "name": "Algorithms", "active": True},
            "target_grade": 2,
            "portfolio_available": True,
            "user_id": 1,
            "unit_id": 7,
        }])[0]]

    def project(self, pid):
        return ontrack.parse_project(_sample_project())

    def unit(self, uid):
        return ontrack.parse_unit(_sample_unit())

    def roles(self, active_only=True):
        return []

    def download_task_sheet(self, unit_id, td_id):
        return {"bytes": b"%PDF-1.4 fake", "content_type": "application/pdf", "filename": "1.1P.pdf"}

    def task_comments(self, pid, td_id):
        self.writes.append(("chat_read", pid, td_id))
        return [{
            "id": 9,
            "comment": "Nice work!",
            "has_attachment": False,
            "type": "text",
            "is_new": True,
            "reply_to_id": None,
            "author": {"id": 2, "first_name": "Tutor", "last_name": "Lee", "email": "tutor@example.edu"},
            "recipient": {"id": 1, "first_name": "Alice", "last_name": "W", "email": "alice@example.edu"},
            "created_at": "2026-10-09T10:00:00Z",
            "recipient_read_time": None,
        }]

    def add_task_comment(self, pid, td_id, message):
        self.writes.append(("chat_send", pid, td_id, message))
        return {
            "id": 10,
            "comment": message,
            "has_attachment": False,
            "type": "text",
            "is_new": False,
            "reply_to_id": None,
            "author": {"id": 1, "first_name": "Alice", "last_name": "W", "email": "alice@example.edu"},
            "recipient": {"id": 2, "first_name": "Tutor", "last_name": "Lee", "email": "tutor@example.edu"},
            "created_at": "2026-10-10T10:00:00Z",
            "recipient_read_time": None,
        }

    def update_task_state(self, pid, td_id, state):
        self.writes.append(("task_state", pid, td_id, state))
        return {"id": 101, "task_definition_id": td_id, "status": state}

    def submit_task(self, pid, td_id, uploads, **kw):
        self.writes.append(("submit", pid, td_id, [u["filename"] for u in uploads], kw))
        return {"id": 101, "task_definition_id": td_id, "status": "ready_for_feedback"}


def test_sync_integrates_into_todo_and_semester(isolated_ontrack, monkeypatch):
    monkeypatch.setattr(ontrack, "load_settings", lambda: {"ontrack_enabled": True})
    monkeypatch.setattr(todo, "load_settings", lambda: {"ontrack_enabled": True, "use_ed": False})
    monkeypatch.setattr(semester, "load_settings", lambda: {"ontrack_enabled": True})

    res = ontrack.sync(FakeClient())
    assert res["units"] == 1 and res["tasks"] == 3

    summary = ontrack.todo_summary(days=7, today=date(2026, 10, 10))
    assert [t["task"] for t in summary["due"]] == ["1.1P"]
    assert [t["task"] for t in summary["attention"]] == ["2.1C"]
    assert summary["unread_chats"][0]["task"] == "1.1P" and summary["unread_chats"][0]["unread_comments"] == 2

    # todo.build 整合 OnTrack 且不要求 Moodle/Ed 登录
    monkeypatch.setattr(ontrack, "todo_summary", lambda days=7: summary)
    built = todo.build(None, [], days=7)
    assert built["ontrack"]["due"][0]["task"] == "1.1P"
    sem = semester.semester_data(built)
    rendered = semester.render(sem)
    assert "OnTrack 任务与未读评论" in rendered and "1.1P" in rendered and "2.1C" in rendered


def test_cli_ontrack_read_and_write_confirmations(isolated_ontrack, monkeypatch, capsys, tmp_path):
    FakeClient.writes = []
    monkeypatch.setattr(ontrack, "OnTrackClient", FakeClient)
    monkeypatch.setattr(ontrack.textextract, "pdf_pages", lambda path: ["Task sheet page 1 content"])

    # 只读命令直接成功
    assert cli.main(["ontrack", "tasks", "FIT2004", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3

    assert cli.main(["ontrack", "read", "FIT2004", "1.1P", "--json"]) == 0
    sheet = json.loads(capsys.readouterr().out)
    assert "Task sheet page 1 content" in sheet["markdown"]

    assert cli.main(["ontrack", "chats", "FIT2004", "--json"]) == 0
    chats = json.loads(capsys.readouterr().out)
    assert any(c["task"] == "1.1P" and c["unread_comments"] == 2 for c in chats)
    assert FakeClient.writes == []

    # 读单任务评论会标已读：非交互无 --confirm 必须拒绝
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["ontrack", "chats", "FIT2004", "1.1P"]) == 1
    assert "--confirm" in capsys.readouterr().err
    assert FakeClient.writes == []

    # --dry-run 只预览
    assert cli.main(["ontrack", "set", "FIT2004", "1.1P", "need_help", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert FakeClient.writes == []

    # 加 --confirm 执行
    assert cli.main(["ontrack", "set", "FIT2004", "1.1P", "need_help", "--confirm", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "need_help"
    assert FakeClient.writes[-1] == ("task_state", 42, 11, "need_help")

    # submit 带文件预览与确认
    pdf = tmp_path / "solution.pdf"
    pdf.write_bytes(b"%PDF-1.4 ok")
    assert cli.main(["ontrack", "submit", "FIT2004", "1.1P", "--file", str(pdf), "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert cli.main(["ontrack", "submit", "FIT2004", "1.1P", "--file", str(pdf), "--confirm", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready_for_feedback"


# ---------------------------------------------------------------- MCP 本地、远程与安全策略

def _rpc(method, params=None):
    return M.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_mcp_ontrack_opt_in_read_and_confirmed_writes(isolated_ontrack, monkeypatch):
    FakeClient.writes = []
    monkeypatch.setattr(ontrack, "OnTrackClient", FakeClient)
    monkeypatch.setattr(ontrack.textextract, "pdf_pages", lambda path: ["Brief text"])

    # 默认关闭：不注册任何 OnTrack 工具
    monkeypatch.setattr(M, "load_settings", lambda: {})
    listed = {t["name"] for t in _rpc("tools/list")["result"]["tools"]}
    assert not (listed & set(student_tools.ONTRACK_READ + student_tools.ONTRACK_WRITE))

    # 开启只读：注册 4 个只读工具，不注册写工具
    monkeypatch.setattr(M, "load_settings", lambda: {"ontrack_enabled": True})
    tools = {t["name"]: t for t in _rpc("tools/list")["result"]["tools"]}
    assert set(student_tools.ONTRACK_READ) <= set(tools)
    assert "ontrack_prepare_action" not in tools and "execute_student_action" not in tools
    # 所有 OnTrack 工具参数都有说明
    for name in student_tools.ONTRACK_READ:
        for param, prop in tools[name]["inputSchema"]["properties"].items():
            assert prop.get("description"), (name, param)

    res = _rpc("tools/call", {"name": "ontrack_task_sheet", "arguments": {"unit": "FIT2004", "task": "1.1P"}})["result"]
    assert "Brief text" in json.loads(res["content"][0]["text"])["markdown"]

    # 开启写操作：两步预览 + 确认，且不含提交作业
    monkeypatch.setattr(M, "load_settings", lambda: {"ontrack_enabled": True, "ontrack_write_enabled": True})
    tools = {t["name"]: t for t in _rpc("tools/list")["result"]["tools"]}
    assert {"ontrack_prepare_action", "execute_student_action"} <= set(tools)
    assert not any("submit" in n for n in tools)

    prep = json.loads(
        _rpc("tools/call", {
            "name": "ontrack_prepare_action",
            "arguments": {"operation": "chat_send", "unit": "FIT2004", "task": "1.1P", "message": "Hi tutor"},
        })["result"]["content"][0]["text"]
    )
    assert prep["review"]["message"] == "Hi tutor"
    assert FakeClient.writes == []

    # 未确认被拒绝
    refused = _rpc("tools/call", {
        "name": "execute_student_action",
        "arguments": {"action_id": prep["action_id"], "review_digest": prep["review_digest"], "user_confirmed": False},
    })["result"]
    assert refused["isError"] and FakeClient.writes == []

    # 确认后执行
    done = json.loads(
        _rpc("tools/call", {
            "name": "execute_student_action",
            "arguments": {"action_id": prep["action_id"], "review_digest": prep["review_digest"], "user_confirmed": True},
        })["result"]["content"][0]["text"]
    )
    assert done["state"] == "done"
    assert FakeClient.writes == [("chat_send", 42, 11, "Hi tutor")]


def test_remote_ontrack_scopes_and_materials_only_blocking(isolated_ontrack, monkeypatch, capsys):
    monkeypatch.setattr(M, "load_settings", lambda: {"ontrack_enabled": True, "ontrack_write_enabled": True})
    # 未授权 ontrack 的远程客户端看不到任何 OnTrack 工具
    base_names = {t["name"] for t in remote.handle_remote({"id": 1, "method": "tools/list"})["result"]["tools"]}
    assert not (base_names & set(student_tools.ONTRACK_READ + student_tools.ONTRACK_WRITE))

    # 只授 ontrack.read 的客户端拿到只读工具，拿不到写工具
    read_names = {
        t["name"]
        for t in remote.handle_remote({"id": 1, "method": "tools/list"}, frozenset({"monash", "ontrack.read"}), "c")["result"]["tools"]
    }
    assert set(student_tools.ONTRACK_READ) <= read_names
    assert "ontrack_prepare_action" not in read_names

    # materials_only 模式下 CLI 与 MCP 一律拒绝 OnTrack
    monkeypatch.setattr(cli, "load_settings", lambda: {"materials_only": True, "ontrack_enabled": True})
    assert cli.main(["ontrack", "status"]) == 1
    assert "只导课程资料模式下不能用" in capsys.readouterr().err
    monkeypatch.setattr(M, "load_settings", lambda: {"materials_only": True, "ontrack_enabled": True})
    assert not ({t["name"] for t in M.tool_list()} & set(student_tools.ONTRACK_READ))


def test_doctor_and_help_cover_ontrack_safely(isolated_ontrack, monkeypatch):
    monkeypatch.setattr(doctor, "load_settings", lambda: {})
    assert doctor.check_ontrack() is None
    assert "monash ontrack" not in help_menu.render(cli=True, settings={})

    monkeypatch.setattr(doctor, "load_settings", lambda: {"ontrack_enabled": True, "ontrack_url": "https://ontrack.example.edu"})
    monkeypatch.setattr(ontrack, "load_settings", lambda: {"ontrack_enabled": True, "ontrack_url": "https://ontrack.example.edu"})
    chk = doctor.check_ontrack()
    assert chk is not None and chk["name"] == "OnTrack" and "@" not in chk["detail"]
    assert "sec-token" not in doctor.scrub("Auth-Token: sec-token; refresh_token=rt-123")
    assert "rt-123" not in doctor.scrub("Auth-Token: sec-token; refresh_token=rt-123")

    helped = help_menu.render(cli=True, settings={"ontrack_enabled": True})
    assert "OnTrack / Doubtfire" in helped and "monash ontrack tasks" in helped
