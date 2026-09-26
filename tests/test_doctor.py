"""monash doctor：每项独立、给出修法、不泄露凭据、一项崩了不影响别的。"""
import json

from monash_study_kit import claude_setup, doctor, jobs
from monash_study_kit.moodlelib import MoodleAuthError


def test_scrub_hides_credentials():
    line = "GET https://x/calendar/export_execute.php?userid=1&authtoken=abc123def failed; Bearer tok.en; MoodleSession=zzz"
    out = doctor.scrub(line)
    assert "abc123def" not in out and "tok.en" not in out and "zzz" not in out
    assert "authtoken=…" in out


def test_moodle_logged_out_says_how_to_fix(monkeypatch):
    class Dead:
        def time_remaining(self):
            raise MoodleAuthError("还没登录 Moodle")
    monkeypatch.setattr(doctor, "MoodleClient", Dead)
    r = doctor.check_moodle()
    assert r["status"] == doctor.FAIL and r["fix"] == "monash login"


def test_ed_without_token(monkeypatch):
    monkeypatch.setattr(jobs, "have_ed_token", lambda: False)
    r = doctor.check_ed()
    assert r["status"] == doctor.FAIL and r["fix"] == "monash login ed"


def test_claude_desktop_config_states(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(claude_setup, "desktop_config_path", lambda: cfg)
    assert doctor.check_claude_desktop()["status"] == doctor.FAIL            # 还没有配置文件
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    assert "没有 monash" in doctor.check_claude_desktop()["detail"]
    cfg.write_text(json.dumps({"mcpServers": {"monash": {"command": str(tmp_path / "gone"), "args": ["mcp"]}}}),
                   encoding="utf-8")
    assert "找不到" in doctor.check_claude_desktop()["detail"]              # 重装后路径变了
    (tmp_path / "monash").write_text("", encoding="utf-8")
    cfg.write_text(json.dumps({"mcpServers": {"monash": {"command": str(tmp_path / "monash"), "args": ["mcp"]}}}),
                   encoding="utf-8")
    assert doctor.check_claude_desktop()["status"] == doctor.OK
    monkeypatch.setattr(claude_setup, "desktop_config_path", lambda: None)
    assert doctor.check_claude_desktop()["status"] == doctor.WARN            # 没装 Desktop 不算错


def test_mcp_log_surfaces_recent_errors_scrubbed(tmp_path, monkeypatch):
    log = tmp_path / "mcp.log"
    monkeypatch.setattr(doctor, "LOG_FILE", log)
    assert doctor.check_mcp_log()["status"] == doctor.OK                     # 还没有日志
    log.write_text("2026-09-27 10:00:00  MCP 启动（0.1.0）\n"
                   "2026-09-27 10:05:00  同步出错：\n"
                   "  Traceback (most recent call last):\n"
                   "2026-09-27 10:06:00  续期失败：authtoken=secret123 timed out\n", encoding="utf-8")
    r = doctor.check_mcp_log()
    assert r["status"] == doctor.WARN and "2026-09-27 10:00" in r["detail"]
    assert "续期失败" in r["detail"] and "secret123" not in r["detail"]


def test_one_crashing_check_does_not_break_the_report(monkeypatch):
    def boom():
        raise RuntimeError("kaput")
    boom.__name__ = "check_boom"
    monkeypatch.setattr(doctor, "CHECKS", [boom, doctor.check_settings])
    report = doctor.run()
    assert [c["status"] for c in report["checks"]] == [doctor.WARN, doctor.OK]
    assert "kaput" in report["checks"][0]["detail"] and report["ok"]
    assert "需要别人帮忙" in doctor.render(report)


def test_skip_lets_mcp_avoid_slow_checks(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", [doctor.check_settings, doctor.check_claude_code])
    assert [c["name"] for c in doctor.run(skip=("check_claude_code",))["checks"]] == ["设置"]
