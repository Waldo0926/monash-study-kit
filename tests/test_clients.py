"""monash connect：接进 Codex、Cursor、VS Code、Windsurf、Gemini CLI。只动 monash 这一项。"""
import json

import pytest

from monash_study_kit import cli, clients

CMD = ["/opt/bin/monash", "mcp"]


@pytest.mark.parametrize("fmt,key", [("json", "mcpServers"), ("vscode", "servers")])
def test_json_keeps_other_content_and_backs_up(tmp_path, fmt, key):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({key: {"other": {"command": "x"}}, "theme": "dark"}), encoding="utf-8")
    backup = clients.write_json(cfg, fmt, CMD)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    want = {"command": "/opt/bin/monash", "args": ["mcp"]}
    if fmt == "vscode":
        want = {"type": "stdio", **want}
    assert data[key]["monash"] == want
    assert data[key]["other"] == {"command": "x"} and data["theme"] == "dark"
    assert "monash" not in json.loads(backup.read_text(encoding="utf-8"))[key]
    assert clients.remove_json(cfg, fmt)
    assert json.loads(cfg.read_text(encoding="utf-8"))[key] == {"other": {"command": "x"}}
    assert not clients.remove_json(cfg, fmt)


def test_json_with_comments_is_left_alone(tmp_path):
    cfg = tmp_path / "mcp.json"
    original = '{\n  // my servers\n  "servers": {}\n}\n'
    cfg.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError):
        clients.write_json(cfg, "vscode", CMD)
    assert cfg.read_text(encoding="utf-8") == original
    assert not clients.remove_json(cfg, "vscode")


def test_json_created_with_folders(tmp_path):
    cfg = tmp_path / ".cursor" / "mcp.json"
    assert clients.write_json(cfg, "json", CMD) is None
    assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["monash"]["args"] == ["mcp"]


def test_toml_replaces_only_our_table(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'model = "o3"\n\n[mcp_servers.monash]\ncommand = "old"\nargs = []\n\n'
        '[mcp_servers.monash.env]\nX = "1"\n\n[mcp_servers.other]\ncommand = "y"\n',
        encoding="utf-8")
    clients.write_toml(cfg, [r"C:\Users\me\monash.exe", "mcp"])
    text = cfg.read_text(encoding="utf-8")
    assert 'model = "o3"' in text and '[mcp_servers.other]\ncommand = "y"' in text
    assert '"old"' not in text and "[mcp_servers.monash.env]" not in text
    assert text.count("[mcp_servers.monash]") == 1
    assert 'command = "C:\\\\Users\\\\me\\\\monash.exe"\nargs = ["mcp"]' in text
    assert clients.remove_toml(cfg)
    assert "monash" not in cfg.read_text(encoding="utf-8")
    assert "[mcp_servers.other]" in cfg.read_text(encoding="utf-8")


def test_toml_new_file(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    assert clients.write_toml(cfg, CMD) is None
    assert cfg.read_text(encoding="utf-8") == '[mcp_servers.monash]\ncommand = "/opt/bin/monash"\nargs = ["mcp"]\n'


def test_connect_falls_back_to_file_and_prints_snippet_on_bad_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(clients, "server_command", lambda: CMD)
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    monkeypatch.setitem(clients.CLIENTS, "cursor",
                        clients.Client("cursor", "Cursor", lambda: tmp_path / "mcp.json", "json", "重启"))
    assert clients.connect("cursor", log=lambda *a: None)
    assert clients.is_connected("cursor")
    (tmp_path / "mcp.json").write_text("{not json", encoding="utf-8")
    out = []
    assert not clients.connect("cursor", log=out.append)
    assert '"mcpServers"' in out[-1] and "/opt/bin/monash" in out[-1]


def test_cli_print_does_not_write(tmp_path, monkeypatch, capsys):
    path = tmp_path / "settings.json"
    monkeypatch.setitem(clients.CLIENTS, "gemini",
                        clients.Client("gemini", "Gemini CLI", lambda: path, "json", ""))
    assert cli.main(["connect", "gemini", "--print"]) == 0
    assert '"mcpServers"' in capsys.readouterr().out and not path.exists()
    with pytest.raises(SystemExit) as e:
        cli.main(["connect", "nope"])
    assert e.value.code == cli.EXIT_USAGE
