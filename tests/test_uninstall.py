"""卸载时删数据：先吊销 Google 授权，吊销失败就不删。"""
from monash_study_kit import cli, google_export as G


def _home(tmp_path):
    home = tmp_path / "kit"
    for folder in (home / "secrets", home / "profiles" / "alice" / "secrets"):
        folder.mkdir(parents=True)
        (folder / "gmail-token.json").write_text("{}")
    (home / "files").mkdir()
    return home


def test_delete_all_data_revokes_every_google_token_first(tmp_path, monkeypatch, capsys):
    home, seen = _home(tmp_path), []
    monkeypatch.setattr(G, "revoke_token", lambda path: seen.append(path))
    assert cli.delete_all_data(home, home / "secrets") is True
    assert not home.exists()
    names = {(p.parent.parent.name, p.name) for p in seen}
    assert ("kit", "gmail-token.json") in names and ("alice", "google-drive-token.json") in names
    assert "edstem.org" in capsys.readouterr().out            # the Ed token has to be deleted by hand


def test_delete_all_data_keeps_everything_when_revoking_fails(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path)

    def fail(path):
        raise G.GoogleError("Google 授权吊销失败，本机令牌已保留，请重试")
    monkeypatch.setattr(G, "revoke_token", fail)
    assert cli.delete_all_data(home, home / "secrets") is False
    assert (home / "secrets" / "gmail-token.json").exists() and (home / "files").exists()
    assert "数据没有删除" in capsys.readouterr().out
