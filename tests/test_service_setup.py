"""在 AI 里一句话接上邮箱或考勤：缺组件自动装、打开登录窗口、接好后同步并刷新工具列表。不联网。"""
import json
import time
from pathlib import Path

import pytest

from monash_study_kit import gmail, paths, remote, service_setup
from monash_study_kit import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent


def wait():
    for _ in range(500):
        if not service_setup._state["running"]:
            return service_setup.status()
        time.sleep(0.01)
    raise AssertionError("connect did not finish")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(service_setup, "_state", {"service": None, "step": None, "running": False,
                                                  "result": None, "error": None})


def test_package_lists_match_the_optional_dependencies():
    tomllib = pytest.importorskip("tomllib")
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    for service in ("gmail", "attendance"):
        declared = {r.split(";")[0].strip() for r in extras[service] if "win32" not in r}
        ours = {r for r in service_setup.PACKAGES[service] if r != "tzdata"}
        assert ours == declared, service


def test_gmail_connect_installs_what_is_missing_then_logs_in(monkeypatch):
    commands, saved, after = [], {}, []
    state = {"installed": False}
    monkeypatch.setattr(service_setup, "missing", lambda s: [] if state["installed"] else ["google_auth_oauthlib"])
    def run(command, timeout):
        commands.append(command)
        state["installed"] = True
    monkeypatch.setattr(service_setup, "_run", run)
    monkeypatch.setattr(service_setup, "_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(gmail, "login", lambda path, manage=False: {"account": "me@example.edu",
                                                                     "mode": "manage" if manage else "sync"})
    monkeypatch.setattr(paths, "save_settings", saved.update)
    service_setup.start("gmail", after=after.append)
    done = wait()
    assert commands == [["/usr/bin/uv", "pip", "install", "--python", service_setup.sys.executable,
                         *service_setup.PACKAGES["gmail"]]]
    assert saved == {"gmail_enabled": True, "gmail_manage_enabled": True} and after == ["gmail"]
    assert done["result"]["mode"] == "manage" and done["step"] == "完成" and "error" not in done


def test_attendance_connect_downloads_the_browser_and_reports_failures(monkeypatch):
    attendance = pytest.importorskip("monash_study_kit.attendance")
    commands = []
    monkeypatch.setattr(service_setup, "missing", lambda s: [])
    monkeypatch.setattr(service_setup, "_run", lambda command, timeout: commands.append(command[-3:]))
    monkeypatch.setattr(attendance, "login", lambda: {"status": "connected", "saved": True})
    monkeypatch.setattr(paths, "save_settings", lambda s: None)
    service_setup.start("attendance")
    assert wait()["result"]["connected"] == "考勤" and commands == [["playwright", "install", "chromium"]]

    def broken(command, timeout):
        raise RuntimeError("安装失败：network")
    monkeypatch.setattr(service_setup, "_run", broken)
    service_setup.start("attendance")
    done = wait()
    assert done["step"] == "失败" and "network" in done["error"]


def test_install_command_falls_back_to_pip_or_explains(monkeypatch):
    monkeypatch.setattr(service_setup, "_uv", lambda: None)
    monkeypatch.setattr(service_setup.importlib.util, "find_spec", lambda name: object() if name == "pip" else None)
    assert service_setup.install_command(["x"])[1:4] == ["-m", "pip", "install"]
    monkeypatch.setattr(service_setup.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match="uv tool install"):
        service_setup.install_command(["x"])


def test_the_ai_can_start_it_but_remote_apps_cannot(monkeypatch):
    monkeypatch.setattr(M, "load_settings", lambda: {})
    tools = {t["name"]: t for t in M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    spec = tools["start_school_service_setup"]
    assert spec["annotations"]["readOnlyHint"] is False
    assert spec["inputSchema"]["properties"]["service"]["enum"] == ["gmail", "attendance"]
    listed = remote.handle_remote({"id": 1, "method": "tools/list"})["result"]["tools"]
    assert "start_school_service_setup" not in {t["name"] for t in listed}
    reply = remote.handle_remote({"id": 2, "method": "tools/call", "params": {"name": "start_school_service_setup",
                                                                          "arguments": {"service": "gmail"}}})
    assert "error" in reply

    started = []
    monkeypatch.setattr(service_setup, "already_connected", lambda s, settings: False)
    monkeypatch.setattr(service_setup, "start", lambda s, read_only=False, after=None: started.append((s, read_only)))
    text = M.call_tool("start_school_service_setup", {"service": "gmail"})["content"][0]["text"]
    assert started == [("gmail", False)] and "不要让用户把密码" in text
    service_setup._state.update(service="gmail", running=True, step="正在安装")
    assert "正在安装" in M.call_tool("start_school_service_setup", {"service": "gmail"})["content"][0]["text"]
    assert M.call_tool("start_school_service_setup", {"service": "nope"})["isError"]


def test_tool_list_change_is_announced_only_on_stdio(monkeypatch, capsys):
    monkeypatch.setitem(M._stdio, "on", False)
    M.notify_tools_changed()
    assert capsys.readouterr().out == ""
    monkeypatch.setitem(M._stdio, "on", True)
    M.notify_tools_changed()
    assert json.loads(capsys.readouterr().out) == {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    init = M.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
    assert init["capabilities"]["tools"] == {"listChanged": True}


def test_setup_tool_disappears_once_both_services_are_connected(monkeypatch):
    def names(settings):
        monkeypatch.setattr(M, "load_settings", lambda: settings)
        return set(M.available_tools())
    assert "start_school_service_setup" in names({})
    assert "start_school_service_setup" in names({"gmail_enabled": True})
    assert "start_school_service_setup" not in names({"gmail_enabled": True, "attendance_enabled": True})


def test_help_offers_to_connect_only_what_is_not_connected():
    from monash_study_kit import help_menu
    assert "帮我接上学校邮箱" in help_menu.render(settings={}) and "帮我接上考勤" in help_menu.render(settings={})
    text = help_menu.render(settings={"gmail_enabled": True})
    assert "帮我接上学校邮箱" not in text and "帮我接上考勤" in text
