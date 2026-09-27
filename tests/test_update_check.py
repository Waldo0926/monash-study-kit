"""新版本提示：版本比较、缓存节奏、网络出错不崩、各处提示。"""
import json
import time

from monash_study_kit import __version__, update_check as U


def _on(tmp_path, monkeypatch):
    monkeypatch.delenv("MONASH_KIT_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(U, "CACHE_FILE", tmp_path / "update-check.json")
    monkeypatch.setattr(U, "HOME", tmp_path)


def test_version_comparison():
    assert U.is_newer("0.10.0", "0.9.3") and U.is_newer("1.0.0", "0.99.99")
    assert not U.is_newer("0.2.0", "0.2.0") and not U.is_newer(None, "0.1.0") and not U.is_newer("0.1.9", "0.2.0")
    assert U.parse("0.2.0rc1") == (0, 2, 0)


def test_checks_at_most_daily_and_survives_network_errors(tmp_path, monkeypatch):
    _on(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(U, "fetch_latest", lambda timeout=5: calls.append(1) or "99.0.0")
    assert U.refresh()["latest"] == "99.0.0"
    U.refresh()
    assert len(calls) == 1                                    # 24 小时内不再查
    assert U.available() == "99.0.0" and "monash update" in U.notice()

    def down(timeout=5):
        raise OSError("no network")
    monkeypatch.setattr(U, "fetch_latest", down)
    assert U.refresh(force=True)["latest"] == "99.0.0"        # 没网就沿用上次的结果
    U.clear()
    assert U.available() is None


def test_disabled_by_env_or_setting(tmp_path, monkeypatch):
    _on(tmp_path, monkeypatch)
    (tmp_path / "update-check.json").write_text(json.dumps({"checked_at": time.time(), "latest": "99.0.0"}),
                                                encoding="utf-8")
    assert U.available() == "99.0.0"
    monkeypatch.setenv("CI", "true")
    assert U.available() is None
    monkeypatch.delenv("CI")
    monkeypatch.setattr(U, "load_settings", lambda: {"update_check": False})
    assert U.available() is None


def test_version_regex_matches_the_real_file():
    from pathlib import Path
    src = (Path(U.__file__).parent / "__init__.py").read_text(encoding="utf-8")
    assert U.VERSION_RE.search(src).group(1) == __version__


def test_notice_reaches_study_todo_and_status(tmp_path, monkeypatch):
    from monash_study_kit import mcp_server as M
    _on(tmp_path, monkeypatch)
    (tmp_path / "update-check.json").write_text(json.dumps({"checked_at": time.time(), "latest": "99.0.0"}),
                                                encoding="utf-8")
    init = M.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
    assert "99.0.0" in init["instructions"]
    r = M.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "study_todo"}})["result"]
    assert any("99.0.0" in n for n in json.loads(r["content"][0]["text"])["notes"])
