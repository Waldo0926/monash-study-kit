"""数据放在哪：location.json 指针、第一次 setup 选位置、monash move 整体搬家。都在临时目录里。"""
import json

import pytest

from monash_study_kit import cli, location as LOC, paths


def test_resolve_home_order(tmp_path):
    default, pointer = tmp_path / "default", tmp_path / "default" / "location.json"
    assert paths.resolve_home({}, pointer, default) == default
    pointer.parent.mkdir()
    pointer.write_text(json.dumps({"home": str(tmp_path / "D")}))
    assert paths.resolve_home({}, pointer, default) == tmp_path / "D"
    assert paths.resolve_home({"MONASH_KIT_HOME": str(tmp_path / "env")}, pointer, default) == tmp_path / "env"
    for bad in ("not json", json.dumps({"home": "relative/path"}), json.dumps(["x"])):
        pointer.write_text(bad)
        assert paths.resolve_home({}, pointer, default) == default


def _home(tmp_path):
    default = tmp_path / "AppData" / "monash-study-kit"
    (default / "data").mkdir(parents=True)
    (default / "data" / "moodle.db").write_bytes(b"db")
    (default / "files" / "FIT1043").mkdir(parents=True)
    (default / "files" / "FIT1043" / "a.pdf").write_bytes(b"%PDF")
    (default / "settings.json").write_text(json.dumps({"files_dir": str(default / "files"), "use_ed": True}))
    return default


def test_move_to_another_drive_and_point_there(tmp_path):
    default = _home(tmp_path)
    pointer = default / "location.json"
    target = tmp_path / "D" / "MonashStudyKit"
    d = LOC.move(default, target, location_file=pointer, default=default, log=lambda *_: None)
    assert d["entries"] == 3
    assert (target / "files" / "FIT1043" / "a.pdf").read_bytes() == b"%PDF"
    assert sorted(p.name for p in default.iterdir()) == ["location.json"]     # C 盘只剩指针
    assert paths.resolve_home({}, pointer, default) == target.resolve()
    # 自定义的课件目录在旧数据目录里，跟着改
    assert json.loads((target / "settings.json").read_text())["files_dir"] == str(target.resolve() / "files")


def test_move_back_to_default_removes_the_pointer(tmp_path):
    default = _home(tmp_path)
    pointer = default / "location.json"
    target = tmp_path / "D" / "MonashStudyKit"
    LOC.move(default, target, location_file=pointer, default=default, log=lambda *_: None)
    LOC.move(target, default, location_file=pointer, default=default, log=lambda *_: None)
    assert not pointer.exists() and (default / "data" / "moodle.db").exists()
    assert not target.exists()                       # 搬空的旧位置删掉


@pytest.mark.parametrize("make_target, err", [
    (lambda h, t: h / "inner", "里面"),
    (lambda h, t: t, "已经有"),
    (lambda h, t: h, "已经在"),
])
def test_move_refuses_unsafe_targets(tmp_path, make_target, err):
    default = _home(tmp_path)
    taken = tmp_path / "taken"
    (taken / "data").mkdir(parents=True)
    with pytest.raises(LOC.MoveError, match=err):
        LOC.move(default, make_target(default, taken), location_file=default / "location.json", default=default)
    assert (default / "data" / "moodle.db").exists()       # 什么都没动


def test_move_needs_an_absolute_path(tmp_path):
    default = _home(tmp_path)
    with pytest.raises(LOC.MoveError, match="完整路径"):
        LOC.move(default, "relative", location_file=default / "location.json", default=default)


def test_recommends_another_drive_when_c_is_nearly_full():
    low = LOC.LOW_SPACE - 1
    cands = [{"path": "C", "free": low, "default": True}, {"path": "D", "free": 50 * 2 ** 30, "default": False},
             {"path": "E", "free": 80 * 2 ** 30, "default": False}]
    assert LOC.recommended(cands) == 2
    cands[0]["free"] = LOC.LOW_SPACE
    assert LOC.recommended(cands) == 0
    assert LOC.recommended(cands[:1]) == 0


def test_ask_accepts_a_number_a_path_or_enter(tmp_path, monkeypatch):
    other = tmp_path / "D" / "MonashStudyKit"
    monkeypatch.setattr(LOC, "candidates", lambda: [
        {"path": paths.DEFAULT_HOME, "free": 1, "default": True}, {"path": other, "free": 99 * 2 ** 30, "default": False}])
    quiet = lambda *_: None  # noqa: E731
    assert LOC.ask(lambda _: "", quiet) == other                        # 回车 = 推荐（C 盘快满了）
    assert LOC.ask(lambda _: "1", quiet) is None                        # 选默认位置
    answers = iter(["relative/dir", str(tmp_path / "E")])
    assert LOC.ask(lambda _: next(answers), quiet) == tmp_path / "E"    # 相对路径会再问一次


def test_cli_move_refuses_when_home_comes_from_the_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MONASH_KIT_HOME", str(tmp_path / "home"))
    assert cli.main(["move", str(tmp_path / "D"), "--yes"]) == 1
    assert "MONASH_KIT_HOME" in capsys.readouterr().err
