"""导出前选文件夹的弹窗：三个平台的调用方式、取消、弹不出来时的退路。不真的弹窗。"""
import subprocess
from pathlib import Path

import pytest

from monash_study_kit import cli, edexport, edlib, folderpick as FP, jobs


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out.encode(), err.encode())


def test_mac_uses_osascript_with_arguments_not_script_text(monkeypatch):
    calls = []
    monkeypatch.setattr(FP, "_system", lambda: "mac")
    monkeypatch.setattr(FP.subprocess, "run", lambda argv, **kw: calls.append(argv) or done(out="/Users/x/我的 资料/\n"))
    assert FP.choose_folder("选文件夹", Path("/Users/x/Downloads")) == Path("/Users/x/我的 资料/")
    argv = calls[0]
    assert argv[0] == "osascript" and argv[-2:] == ["选文件夹", "/Users/x/Downloads"]
    assert not any("Downloads" in a for a in argv[:-1])


def test_mac_cancel_and_failure(monkeypatch):
    monkeypatch.setattr(FP, "_system", lambda: "mac")
    monkeypatch.setattr(FP.subprocess, "run", lambda argv, **kw: done(1, err="User canceled. (-128)"))
    assert FP.choose_folder("p", Path("/tmp")) is None
    monkeypatch.setattr(FP.subprocess, "run", lambda argv, **kw: done(1, err="no window server"))
    with pytest.raises(FP.Unavailable):
        FP.choose_folder("p", Path("/tmp"))


def test_windows_passes_paths_through_environment(monkeypatch):
    seen = {}
    monkeypatch.setattr(FP, "_system", lambda: "windows")
    monkeypatch.setattr(FP.shutil, "which", lambda name: "C:\\Windows\\powershell.exe" if name == "powershell" else None)

    def run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        return done(out="D:\\课程 资料\r\n")

    monkeypatch.setattr(FP.subprocess, "run", run)
    assert FP.choose_folder("选文件夹", Path("C:/Users/学生 A/Downloads")) == Path("D:\\课程 资料")
    assert seen["argv"][:4] == ["C:\\Windows\\powershell.exe", "-NoProfile", "-NonInteractive", "-STA"]
    assert "Downloads" not in " ".join(seen["argv"])
    assert seen["env"]["MONASH_PICK_DEFAULT"].endswith("Downloads") and seen["env"]["MONASH_PICK_PROMPT"] == "选文件夹"
    monkeypatch.setattr(FP.subprocess, "run", lambda argv, **kw: done(out=""))
    assert FP.choose_folder("p", Path("C:/")) is None  # 点了取消


def test_linux_needs_a_desktop_and_a_picker(monkeypatch):
    monkeypatch.setattr(FP, "_system", lambda: "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(FP.Unavailable):
        FP.choose_folder("p", Path("/home/x"))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(FP.shutil, "which", lambda name: None)
    with pytest.raises(FP.Unavailable):
        FP.choose_folder("p", Path("/home/x"))
    monkeypatch.setattr(FP.shutil, "which", lambda name: "/usr/bin/zenity" if name == "zenity" else None)
    monkeypatch.setattr(FP.subprocess, "run", lambda argv, **kw: done(1))
    assert FP.choose_folder("p", Path("/home/x")) is None


@pytest.fixture
def export_cli(tmp_path, monkeypatch):
    saved = {}
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(edexport, "default_dir", lambda: downloads)
    monkeypatch.setattr(cli, "load_settings", lambda: {"export_dir": saved.get("export_dir", "")})
    monkeypatch.setattr(cli, "save_settings", saved.update)
    monkeypatch.setattr(cli, "is_terminal", lambda: True)
    return tmp_path, downloads, saved


def parse(*argv):
    return cli.build_parser().parse_args(["export", "FIT1043", *argv])


def test_picker_choice_is_used_and_remembered(export_cli, monkeypatch):
    tmp, downloads, saved = export_cli
    chosen = tmp / "选好的"
    chosen.mkdir()
    initials = []
    monkeypatch.setattr(FP, "choose_folder", lambda prompt, initial: initials.append(initial) or chosen)
    assert cli.export_destination(parse()) == chosen
    assert initials == [downloads] and saved == {"export_dir": str(chosen)}
    cli.export_destination(parse())
    assert initials[-1] == chosen  # 下次默认就是上次选的


def test_to_no_ask_json_and_unavailable_skip_the_dialog(export_cli, monkeypatch, capsys):
    tmp, downloads, saved = export_cli
    monkeypatch.setattr(FP, "choose_folder", lambda *a: pytest.fail("不该弹窗"))
    assert cli.export_destination(parse("--to", str(tmp / "x"))) == tmp / "x"
    assert cli.export_destination(parse("--no-ask")) == downloads
    assert cli.export_destination(parse("--json")) == downloads
    monkeypatch.setattr(cli, "is_terminal", lambda: False)
    assert cli.export_destination(parse()) == downloads
    monkeypatch.setattr(cli, "is_terminal", lambda: True)

    def unavailable(*a):
        raise FP.Unavailable("no desktop")

    monkeypatch.setattr(FP, "choose_folder", unavailable)
    assert cli.export_destination(parse()) == downloads
    assert "弹不出选择窗口" in capsys.readouterr().err and saved == {}


def test_cancel_stops_before_syncing(export_cli, monkeypatch, capsys):
    monkeypatch.setattr(FP, "choose_folder", lambda *a: None)
    monkeypatch.setattr(cli, "sync_export_course", lambda *a, **kw: pytest.fail("取消后不该同步"))
    args = parse("--sync")
    assert args.fn(args) == 1
    assert "已取消导出" in capsys.readouterr().err


def test_export_sync_only_fetches_ed_lessons(monkeypatch):
    """导出只要课程资料：--sync 不抓 Ed 论坛帖子。"""
    calls = []

    class FakeEd:
        def courses(self):
            return [{"id": 7, "code": "FIT1043"}]

    monkeypatch.setattr(edlib, "EdClient", FakeEd)
    monkeypatch.setattr(jobs, "load_settings", lambda: {"materials_only": False})
    monkeypatch.setattr(jobs.edsync, "sync", lambda **kw: pytest.fail("不能同步论坛帖子"))
    monkeypatch.setattr(jobs.edsync, "upsert_courses", lambda *a: None)
    monkeypatch.setattr(jobs.edsync, "auto_track", lambda *a: None)
    monkeypatch.setattr(edlib, "db_connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(jobs.lessons, "sync_lessons", lambda conn, client, cid, log: calls.append(cid))
    assert jobs.sync_ed(log=lambda _: None, course_code="FIT1043", lessons_only=True) == {"new_threads": 0, "new_replies": 0}
    assert calls == [7]


def test_windows_downloads_uses_the_registered_folder(tmp_path, monkeypatch):
    moved = tmp_path / "D-drive" / "下载"
    moved.mkdir(parents=True)
    monkeypatch.setattr(edexport.sys, "platform", "win32")
    monkeypatch.setattr(edexport, "_windows_downloads", lambda: moved)
    assert edexport.default_dir() == moved
    monkeypatch.setattr(edexport, "_windows_downloads", lambda: None)
    monkeypatch.setattr(edexport.Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setattr(edexport.Path, "cwd", lambda: tmp_path)
    assert edexport.default_dir() == tmp_path


@pytest.mark.skipif(FP.sys.platform != "win32", reason="只在 Windows 上检查 PowerShell 脚本语法")
def test_windows_script_parses_in_powershell():
    check = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseInput("
             "$env:MONASH_PICK_SCRIPT, [ref]$null, [ref]$e); $e.Count")
    import os
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", check],
                       capture_output=True, text=True, env={**os.environ, "MONASH_PICK_SCRIPT": FP._WINDOWS})
    assert r.returncode == 0 and r.stdout.strip() == "0", r.stderr
