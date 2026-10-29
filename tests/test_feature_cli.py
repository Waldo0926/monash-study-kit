import argparse

import pytest

from monash_study_kit import cli, library, remote


@pytest.mark.parametrize("argv,action", [
    (["library", "--port", "0"], None),
    (["remote", "serve", "--public-url", "https://study.example.org", "--no-background"], "serve"),
    (["remote", "pair"], "pair"), (["remote", "clients"], "clients"),
    (["remote", "revoke", "--all"], "revoke"),
])
def test_feature_commands_parse(argv, action):
    args = cli.build_parser().parse_args(argv)
    assert callable(args.fn)
    if action:
        assert args.action == action


def test_revoke_requires_an_explicit_target():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["remote", "revoke"])


def test_pair_list_revoke_use_isolated_public_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(remote, "SECRETS_DIR", tmp_path / "public-secrets")
    assert remote.command(argparse.Namespace(action="pair")) == 0
    assert "Pairing code:" in capsys.readouterr().out
    conn = remote.db()
    conn.execute("INSERT INTO clients VALUES ('test','Fixture client','[]',0)")
    token = remote.issue_tokens(conn, "test")
    conn.close()
    remote.command(argparse.Namespace(action="clients"))
    assert "Fixture client" in capsys.readouterr().out
    remote.command(argparse.Namespace(action="revoke", all=True, client=None))
    conn = remote.db()
    try:
        assert not remote.valid_access(conn, token["access_token"])
        assert not remote.pairing_open(conn)
    finally:
        conn.close()


def test_library_not_opened_unless_requested(monkeypatch):
    class Server:
        server_port = 9000
        def serve_forever(self):
            pass
        def server_close(self):
            pass
    monkeypatch.setattr(library, "make_server", lambda port: Server())
    def no_browser(*a):
        raise AssertionError("Browser must be opt-in")
    monkeypatch.setattr(cli.webbrowser, "open", no_browser)
    assert library.command(argparse.Namespace(port=9000, open=False)) == 0
