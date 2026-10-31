"""有的课不用 Ed：设了 use_ed=false 以后，同步、待办、体检、AI 里都不再提 Ed，别的功能照常。"""
from monash_study_kit import doctor, help_menu, jobs, todo
from monash_study_kit import mcp_server as M
from monash_study_kit.moodlelib import MoodleAuthError


def rpc(method, params=None):
    return M.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_ed_tools_and_help_are_hidden_only_when_ed_is_off(monkeypatch):
    monkeypatch.setattr(M, "load_settings", lambda: {})
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert set(M.ED_TOOLS) <= names and "不用 Ed" not in rpc("initialize")["result"]["instructions"]
    monkeypatch.setattr(M, "load_settings", lambda: {"use_ed": False})
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert not set(M.ED_TOOLS) & names and "get_study_todo" in names and "search_content" in names
    assert "不用 Ed" in rpc("initialize")["result"]["instructions"]
    assert "error" in rpc("tools/call", {"name": "list_ed_updates", "arguments": {}})
    assert "Ed 论坛" in help_menu.render(settings={}) and "Ed 论坛" not in help_menu.render(settings={"use_ed": False})


def test_sync_and_todo_do_not_nag_about_ed(monkeypatch):
    def no_moodle(log):
        raise MoodleAuthError("not logged in")
    monkeypatch.setattr(jobs, "have_ed_token", lambda: False)
    monkeypatch.setattr(jobs, "sync_moodle", no_moodle)
    monkeypatch.setattr(jobs, "load_settings", lambda: {"use_ed": True, "web_notes": False})
    assert "ed" in jobs.sync_all(log=lambda *a: None)["errors"]
    monkeypatch.setattr(jobs, "load_settings", lambda: {"use_ed": False, "web_notes": False})
    assert "ed" not in jobs.sync_all(log=lambda *a: None)["errors"]

    monkeypatch.setattr(todo, "_ed_conn", lambda: None)
    monkeypatch.setattr(todo, "load_settings", lambda: {"use_ed": False})
    assert not any("Ed" in n for n in todo.build(None, [], 7)["notes"])
    monkeypatch.setattr(todo, "load_settings", lambda: {})
    assert any("Ed" in n for n in todo.build(None, [], 7)["notes"])


def test_doctor_does_not_fail_on_a_missing_ed_token_when_ed_is_off(monkeypatch):
    monkeypatch.setattr(doctor.jobs, "have_ed_token", lambda: False)
    monkeypatch.setattr(doctor, "load_settings", lambda: {})
    assert doctor.check_ed()["status"] == doctor.FAIL
    monkeypatch.setattr(doctor, "load_settings", lambda: {"use_ed": False})
    assert doctor.check_ed()["status"] == doctor.OK
