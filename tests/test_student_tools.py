"""本地 MCP 的 Gmail 和考勤：按设置注册、写操作先预览再确认、远程一律拒绝、命令行能用。"""
import json

import pytest

from monash_study_kit import cli, gmail, gmail_tools, help_menu, remote, student_tools
from monash_study_kit import mcp_server as M

WRITE = {"gmail_prepare_action", "attendance_prepare_submit", "execute_student_action"}


def use(monkeypatch, **flags):
    monkeypatch.setattr(M, "load_settings", lambda: dict(flags))


def rpc(method, params=None):
    return M.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def names():
    return {t["name"] for t in rpc("tools/list")["result"]["tools"]}


def call(name, arguments):
    return rpc("tools/call", {"name": name, "arguments": arguments})


def test_nothing_is_registered_by_default(monkeypatch):
    use(monkeypatch)
    assert not (names() & student_tools.NAMES)
    assert "学校邮箱" not in rpc("initialize")["result"]["instructions"]


def test_read_tools_follow_settings_and_writes_need_their_own_switch(monkeypatch):
    use(monkeypatch, gmail_enabled=True)
    listed = names()
    assert set(gmail_tools.TOOLS) <= listed and "get_gmail_inbox_todo" in listed
    assert not (listed & WRITE) and "attendance_status" not in listed

    use(monkeypatch, attendance_enabled=True)
    listed = names()
    assert {"attendance_status", "attendance_overview"} <= listed
    assert not (listed & WRITE) and "attendance_sync" not in listed

    # 写操作的开关单独开没用，要先开对应的只读功能
    use(monkeypatch, gmail_manage_enabled=True, attendance_submit_enabled=True)
    assert not (names() & student_tools.NAMES)

    use(monkeypatch, gmail_enabled=True, gmail_manage_enabled=True)
    assert {"gmail_prepare_action", "execute_student_action"} <= names()
    assert "attendance_prepare_submit" not in names()

    use(monkeypatch, attendance_enabled=True, attendance_submit_enabled=True)
    assert {"attendance_prepare_submit", "execute_student_action"} <= names()
    tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
    assert tools["execute_student_action"]["annotations"]["readOnlyHint"] is False


def test_instructions_point_mail_questions_at_this_server(monkeypatch):
    use(monkeypatch, gmail_enabled=True)
    text = rpc("initialize")["result"]["instructions"]
    assert "学校邮箱" in text and "不要改用其他通用 Gmail 连接器" in text and "get_gmail_inbox_todo" in text
    assert "execute_student_action" not in text
    use(monkeypatch, gmail_enabled=True, gmail_manage_enabled=True)
    text = rpc("initialize")["result"]["instructions"]
    assert "完整预览" in text and "不能自己替用户确认" in text


def test_old_remote_grants_get_no_student_tools_even_when_enabled_locally(monkeypatch):
    # 只有 monash 权限的客户端（包括升级前配对的）看不到也调不了邮件和考勤
    use(monkeypatch, gmail_enabled=True, gmail_manage_enabled=True,
        attendance_enabled=True, attendance_submit_enabled=True)
    listed = remote.handle_remote({"id": 1, "method": "tools/list"})["result"]["tools"]
    assert not ({t["name"] for t in listed} & student_tools.NAMES)
    for name in student_tools.NAMES:
        reply = remote.handle_remote({"id": 2, "method": "tools/call", "params": {"name": name, "arguments": {}}})
        assert "error" in reply, name
    init = remote.handle_remote({"id": 3, "method": "initialize", "params": {}})["result"]["instructions"]
    assert "学校邮箱" not in init


class Mailbox:
    """假的 Gmail：只记录写操作，读操作给固定内容。"""
    writes = []

    def get(self, path, **params):
        if path == "profile":
            return {"emailAddress": "me@example.edu"}
        if path == "labels":
            return {"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "UNREAD", "name": "UNREAD"}]}
        return {"id": "m1", "labelIds": ["UNREAD"], "payload": {
            "mimeType": "text/plain", "headers": [{"name": "Subject", "value": "Exchange"}], "body": {}}}

    def modify_labels(self, message_id, *, add=(), remove=()):
        self.writes.append((message_id, list(add), list(remove)))
        return {"id": message_id}


def test_mail_write_runs_only_after_preview_and_explicit_confirmation(monkeypatch):
    Mailbox.writes = []
    monkeypatch.setattr(gmail, "Client", Mailbox)
    use(monkeypatch, gmail_enabled=True, gmail_manage_enabled=True)
    reply = call("gmail_prepare_action", {"operation": "labels", "message_id": "m1", "remove": "收件箱"})
    review = json.loads(reply["result"]["content"][0]["text"])
    assert review["review"]["changes"] == ["-INBOX"] and review["review"]["message"] == "Exchange"
    assert Mailbox.writes == []

    ticket = {"action_id": review["action_id"], "review_digest": review["review_digest"]}
    refused = call("execute_student_action", {**ticket, "user_confirmed": False})["result"]
    assert refused["isError"] and Mailbox.writes == []
    wrong = call("execute_student_action", {**ticket, "review_digest": "0" * 64, "user_confirmed": True})["result"]
    assert wrong["isError"] and Mailbox.writes == []

    done = json.loads(call("execute_student_action", {**ticket, "user_confirmed": True})
                      ["result"]["content"][0]["text"])
    assert done["state"] == "done" and Mailbox.writes == [("m1", [], ["INBOX"])]
    again = json.loads(call("execute_student_action", {**ticket, "user_confirmed": True})
                       ["result"]["content"][0]["text"])
    assert again["state"] == "done" and len(Mailbox.writes) == 1

    # 预览之后关掉了邮箱操作，凭据就用不了
    review = json.loads(call("gmail_prepare_action", {"operation": "labels", "message_id": "m1", "add": "星标"})
                        ["result"]["content"][0]["text"])
    use(monkeypatch, gmail_enabled=True)
    assert "error" in call("execute_student_action", {"action_id": review["action_id"],
                                                       "review_digest": review["review_digest"],
                                                       "user_confirmed": True})
    assert len(Mailbox.writes) == 1


def test_expired_gmail_login_tells_the_user_what_to_run(monkeypatch):
    class Expired:
        def get(self, *a, **kw):
            raise gmail.GmailAuthError("Gmail authorization expired")
    monkeypatch.setattr(gmail, "Client", Expired)
    use(monkeypatch, gmail_enabled=True, gmail_manage_enabled=True)
    result = call("gmail_prepare_action", {"operation": "labels", "message_id": "m1", "add": "STARRED"})["result"]
    assert result["isError"] and "monash gmail login" in result["content"][0]["text"]


def test_attendance_tools_read_the_saved_records(monkeypatch):
    attendance = pytest.importorskip("monash_study_kit.attendance")
    monkeypatch.setattr(attendance, "status", lambda: {"configured": True, "cached_records": 3})
    use(monkeypatch, attendance_enabled=True)
    result = call("attendance_status", {})["result"]
    assert json.loads(result["content"][0]["text"])["cached_records"] == 3

    def expired(days=14):
        raise attendance.AttendanceAuthError("Attendance session expired")
    monkeypatch.setattr(attendance, "overview", expired)
    result = call("attendance_overview", {"days": 7})["result"]
    assert result["isError"] and "monash attendance login" in result["content"][0]["text"]


def test_help_lists_mail_and_attendance_only_when_enabled():
    plain = help_menu.render(cli=True, settings={})
    assert "学校邮箱" not in plain and "考勤" not in plain
    text = help_menu.render(cli=True, settings={"gmail_enabled": True, "attendance_enabled": True})
    assert "monash gmail todo" in text and "monash attendance sync" in text
    assert "邮箱操作" not in text and "签到提交" not in text
    text = help_menu.render(cli=False, settings={"gmail_enabled": True, "gmail_manage_enabled": True,
                                                "attendance_enabled": True, "attendance_submit_enabled": True})
    assert "邮箱操作（先预览，确认后才执行）" in text and "签到提交" in text and "monash gmail" not in text
    assert text.index("考勤") < text.index("■ 维护")


def test_cli_gmail_todo_reads_the_cache(monkeypatch, capsys, tmp_path):
    store = gmail.Store(tmp_path / "gmail.db")
    monkeypatch.setattr(gmail, "Store", lambda: store)
    args = cli.build_parser().parse_args(["gmail", "todo", "--days", "3"])
    assert args.fn(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["days"] == 3 and out["items"] == []


def test_cli_attendance_login_status_and_expired_session(monkeypatch, capsys):
    attendance = pytest.importorskip("monash_study_kit.attendance")
    saved = {}
    monkeypatch.setattr("monash_study_kit.cli_attendance.save_settings", saved.update)
    monkeypatch.setattr(attendance, "login", lambda timeout=600: {"status": "connected", "saved": True})
    args = cli.build_parser().parse_args(["attendance", "login"])
    assert args.fn(args) == 0 and saved == {"attendance_enabled": True}
    capsys.readouterr()

    monkeypatch.setattr(attendance, "status", lambda: {"configured": False, "cached_records": 0})
    args = cli.build_parser().parse_args(["attendance", "status"])
    assert args.fn(args) == 0 and json.loads(capsys.readouterr().out)["configured"] is False

    def expired(days=7):
        raise attendance.AttendanceAuthError("Attendance session expired")
    monkeypatch.setattr(attendance, "sync", expired)
    args = cli.build_parser().parse_args(["attendance", "sync", "--days", "3"])
    assert args.fn(args) == cli.EXIT_AUTH
    assert "monash attendance login" in capsys.readouterr().err


# ---------------------------------------------------------------- 邮件后台同步

class FakeStore:
    def __init__(self, query=None):
        self.query, self.closed = query, False

    def state(self, key, default=None):
        return self.query if key == "query" else default

    def close(self):
        self.closed = True


def test_background_mail_sync_only_when_enabled(monkeypatch):
    started = []
    monkeypatch.setattr(M, "_sync_mail", lambda: started.append(1))
    monkeypatch.setitem(M._mail, "next", 0.0)
    monkeypatch.setitem(M._mail, "thread", None)
    use(monkeypatch, gmail_enabled=False, gmail_sync_minutes=30)
    assert M.start_mail_sync() is False
    use(monkeypatch, gmail_enabled=True, gmail_sync_minutes=0)
    assert M.start_mail_sync() is False
    use(monkeypatch, gmail_enabled=True, gmail_sync_minutes=30)
    assert M.start_mail_sync() is True
    M._mail["thread"].join(5)
    assert started == [1] and M.start_mail_sync() is False        # 30 分钟内不再跑


def test_background_mail_sync_keeps_query_resumes_and_indexes_attachments(monkeypatch):
    calls = []
    store = FakeStore("newer_than:30d")
    monkeypatch.setattr(gmail, "Store", lambda: store)
    monkeypatch.setattr(gmail, "Client", lambda: "client")
    monkeypatch.setattr(gmail, "sync_attachments", lambda c, st, limit: calls.append(("attachments", limit)))
    status = {"value": "more_pending"}
    def sync(client, st, *, query, page_limit):
        calls.append(("sync", query, page_limit))
        return {"status": status["value"], "processed": 3}
    monkeypatch.setattr(gmail, "sync", sync)
    monkeypatch.setitem(M._mail, "next", 999.0)
    M._sync_mail()
    assert calls == [("sync", "newer_than:30d", 3)] and M._mail["next"] == 0.0 and store.closed
    status["value"] = "ok"
    M._sync_mail()
    assert calls[-1] == ("attachments", 5)


def test_background_mail_sync_logs_expired_login_once(monkeypatch):
    logged = []
    monkeypatch.setattr(M, "log", logged.append)
    monkeypatch.setattr(gmail, "Store", lambda: FakeStore())
    monkeypatch.setattr(gmail, "Client", lambda: None)
    def expired(*a, **kw):
        raise gmail.GmailAuthError("expired")
    monkeypatch.setattr(gmail, "sync", expired)
    monkeypatch.setitem(M._mail, "auth_logged", False)
    M._sync_mail()
    M._sync_mail()
    assert len(logged) == 1 and "重新授权" in logged[0]


# ---------------------------------------------------------------- 远程按客户端授权

def remote_call(name, arguments, scopes, owner="client-a", id_=1):
    return remote.handle_remote({"id": id_, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
                                frozenset(scopes), owner)


def remote_names(scopes):
    return {t["name"] for t in remote.handle_remote({"id": 1, "method": "tools/list"}, frozenset(scopes), "c")
            ["result"]["tools"]}


def test_remote_client_gets_only_what_it_was_granted_and_the_server_allows(monkeypatch):
    use(monkeypatch, gmail_enabled=True, gmail_manage_enabled=True, attendance_enabled=True)
    names = remote_names({"monash", "gmail.read"})
    assert {"gmail_search", "gmail_inbox_todo", "student_job_status", "list_moodle_due"} <= names
    assert not names & {"gmail_prepare_action", "execute_student_action", "attendance_status"}
    assert not names & set(gmail_tools.TOOLS)          # 本地专用的名字不出现在远程
    assert "attendance_status" in remote_names({"monash", "attendance.read"})
    # 客户端授权了，但服务器没开签到提交：拿不到
    assert "attendance_prepare_submit" not in remote_names({"monash", "attendance.read", "attendance.submit"})
    # 服务器关掉 Gmail 以后，原来授权过的客户端也拿不到
    use(monkeypatch)
    assert not remote_names({"monash", "gmail.read", "gmail.manage"}) & remote_tools()
    reply = remote_call("gmail_inbox_todo", {}, {"monash", "gmail.read"})
    assert reply["error"]["code"] == -32602


def remote_tools():
    return set(student_tools.REMOTE_NAMES) | student_tools.NAMES


def test_remote_inbox_todo_and_instructions(monkeypatch, tmp_path):
    use(monkeypatch, gmail_enabled=True)
    store = gmail.Store(tmp_path / "gmail.db")
    monkeypatch.setattr(gmail, "Store", lambda: store)
    monkeypatch.setattr(gmail, "Client", Mailbox)
    result = remote_call("gmail_inbox_todo", {"days": 3}, {"monash", "gmail.read"})["result"]
    assert json.loads(result["content"][0]["text"])["days"] == 3
    bad = remote_call("gmail_inbox_todo", {"days": "3"}, {"monash", "gmail.read"})["result"]
    assert bad["isError"] and "Invalid argument type" in bad["content"][0]["text"]
    init = remote.handle_remote({"id": 1, "method": "initialize", "params": {}}, frozenset({"monash", "gmail.read"}), "c")
    assert "学校邮箱" in init["result"]["instructions"] and "gmail_inbox_todo" in init["result"]["instructions"]


def test_remote_write_needs_confirmation_and_tickets_belong_to_one_client(monkeypatch):
    Mailbox.writes = []
    monkeypatch.setattr(gmail, "Client", Mailbox)
    use(monkeypatch, gmail_enabled=True, gmail_manage_enabled=True)
    scopes = {"monash", "gmail.manage"}
    reply = remote_call("gmail_prepare_action", {"operation": "labels", "message_id": "m1", "remove": "INBOX"}, scopes)
    review = json.loads(reply["result"]["content"][0]["text"])
    ticket = {"action_id": review["action_id"], "review_digest": review["review_digest"], "user_confirmed": True}
    other = remote_call("execute_student_action", ticket, scopes, owner="client-b")["result"]
    assert other["isError"] and Mailbox.writes == []
    read_only = remote_call("execute_student_action", ticket, {"monash", "gmail.read"})
    assert "error" in read_only and Mailbox.writes == []
    done = json.loads(remote_call("execute_student_action", ticket, scopes)["result"]["content"][0]["text"])
    assert done["state"] == "done" and Mailbox.writes == [("m1", [], ["INBOX"])]
