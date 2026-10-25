"""把 monash MCP 接进 Claude 以外的 AI 软件：Codex、Cursor、VS Code、Windsurf、Gemini CLI。

都是本地 stdio MCP，只是每家配置文件的位置和格式不一样。原则：
- 只改 monash 这一项，文件里别的内容原样保留，改之前备份。
- 软件自己有 `xxx mcp add` 命令的（Codex），优先用它；没有或失败了再直接改文件。
- 文件不是合法 JSON（比如带注释）就不动它，打印出要加的那段让用户自己贴。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .claude_setup import SERVER_NAME, server_command


@dataclass
class Client:
    key: str
    name: str
    path: Callable[[], Path]
    fmt: str                 # "json"：mcpServers；"vscode"：servers + type；"toml"：Codex
    after: str               # 接好以后要做什么才能用上


def _vscode_user_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Code" / "User"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Code" / "User"


def _codex_home() -> Path:
    return Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"


CLIENTS: dict[str, Client] = {c.key: c for c in [
    Client("codex", "Codex", lambda: _codex_home() / "config.toml", "toml",
           "新开一个 Codex 会话，输入 /mcp 能看到 monash。"),
    Client("cursor", "Cursor", lambda: Path.home() / ".cursor" / "mcp.json", "json",
           "重启 Cursor（或 Cmd/Ctrl+Shift+P → Developer: Reload Window），在 Settings → MCP 里能看到 monash。"),
    Client("vscode", "VS Code", lambda: _vscode_user_dir() / "mcp.json", "vscode",
           "在 VS Code 的 Copilot Chat 里切到 Agent 模式，工具列表里能看到 monash。第一次用时 VS Code 会问你是否信任它。"),
    Client("windsurf", "Windsurf", lambda: Path.home() / ".codeium" / "windsurf" / "mcp_config.json", "json",
           "重启 Windsurf，或在 Cascade 面板的 MCP 图标里点刷新。"),
    Client("gemini", "Gemini CLI", lambda: Path.home() / ".gemini" / "settings.json", "json",
           "重新打开 gemini，输入 /mcp 能看到 monash。"),
]}


def entry(fmt: str, command: list[str]) -> dict:
    e = {"command": command[0], "args": command[1:]}
    return {"type": "stdio", **e} if fmt == "vscode" else e


def snippet(client: Client, command: list[str]) -> str:
    """要加进配置文件的那一段，给 --print 和改不了文件时用。"""
    if client.fmt == "toml":
        return _toml_table(command)
    key = "servers" if client.fmt == "vscode" else "mcpServers"
    return json.dumps({key: {SERVER_NAME: entry(client.fmt, command)}}, ensure_ascii=False, indent=2)


def detected(client: Client) -> bool:
    """配置文件或它所在的文件夹在，就当这个软件装过。"""
    p = client.path()
    return p.exists() or p.parent.exists()


def _backup(path: Path, raw: str) -> Path:
    backup = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d%H%M%S')}")
    backup.write_text(raw, encoding="utf-8")
    return backup


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- JSON（Cursor、VS Code、Windsurf、Gemini CLI）

def write_json(path: Path, fmt: str, command: list[str]) -> Path | None:
    """加上（或更新）monash 这一项，其他内容原样保留。返回备份路径。文件不是合法 JSON 时抛 ValueError。"""
    cfg, backup = {}, None
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        cfg = json.loads(raw) if raw.strip() else {}
        if not isinstance(cfg, dict):
            raise ValueError(f"{path} 不是一个 JSON 对象")
        backup = _backup(path, raw)
    key = "servers" if fmt == "vscode" else "mcpServers"
    cfg.setdefault(key, {})[SERVER_NAME] = entry(fmt, command)
    _atomic_write(path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return backup


def remove_json(path: Path, fmt: str) -> bool:
    if not path.exists():
        return False
    try:
        cfg = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        return False
    key = "servers" if fmt == "vscode" else "mcpServers"
    if not isinstance(cfg, dict) or SERVER_NAME not in (cfg.get(key) or {}):
        return False
    del cfg[key][SERVER_NAME]
    _atomic_write(path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return True


# ---------------------------------------------------------------- TOML（Codex）
# Python 3.10 没有 tomllib，这里只按行处理 [mcp_servers.monash] 这一张表，文件其他部分一个字不动。

_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)    # JSON 字符串的转义规则在这里和 TOML basic string 一样


def _toml_table(command: list[str]) -> str:
    args = ", ".join(_toml_str(a) for a in command[1:])
    return f"[mcp_servers.{SERVER_NAME}]\ncommand = {_toml_str(command[0])}\nargs = [{args}]\n"


def _strip_table(text: str) -> tuple[str, bool]:
    """去掉 [mcp_servers.monash] 和它的子表，返回 (剩下的内容, 有没有去掉东西)。"""
    ours = f"mcp_servers.{SERVER_NAME}"
    out, skipping, found = [], False, False
    for line in text.splitlines(keepends=True):
        m = _HEADER.match(line)
        if m:
            name = m.group(1).replace('"', "").replace(" ", "")
            skipping = name == ours or name.startswith(ours + ".")
            found = found or skipping
        if not skipping:
            out.append(line)
    return "".join(out), found


def write_toml(path: Path, command: list[str]) -> Path | None:
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    backup = _backup(path, raw) if path.exists() else None
    rest, _ = _strip_table(raw)
    rest = rest.rstrip("\n")
    _atomic_write(path, (rest + "\n\n" if rest else "") + _toml_table(command))
    return backup


def remove_toml(path: Path) -> bool:
    if not path.exists():
        return False
    rest, found = _strip_table(path.read_text(encoding="utf-8"))
    if found:
        _atomic_write(path, rest.rstrip("\n") + "\n" if rest.strip() else "")
    return found


# ---------------------------------------------------------------- 对外

def _codex_cli(command: list[str]) -> bool:
    codex = shutil.which("codex")
    if not codex:
        return False
    subprocess.run([codex, "mcp", "remove", SERVER_NAME], capture_output=True)
    r = subprocess.run([codex, "mcp", "add", SERVER_NAME, "--", *command], capture_output=True, text=True)
    return r.returncode == 0


def connect(key: str, log=print) -> bool:
    client = CLIENTS[key]
    command = server_command()
    path = client.path()
    try:
        if client.fmt == "toml":
            if _codex_cli(command):
                log(f"已用 codex mcp add 把 monash 加进 {client.name}")
                log(client.after)
                return True
            backup = write_toml(path, command)
        else:
            backup = write_json(path, client.fmt, command)
    except (ValueError, OSError) as e:
        log(f"没能自动改 {path}：{e}")
        log("请打开这个文件，把下面这段合并进去（已有的内容别删）：\n")
        log(snippet(client, command))
        return False
    log(f"已把 monash 加进 {client.name}（{path}）" + (f"，原配置备份在 {backup.name}" if backup else ""))
    log(client.after)
    return True


def disconnect(key: str) -> bool:
    client = CLIENTS[key]
    path = client.path()
    if client.fmt == "toml":
        codex = shutil.which("codex")
        if codex and subprocess.run([codex, "mcp", "remove", SERVER_NAME], capture_output=True).returncode == 0:
            remove_toml(path)
            return True
        return remove_toml(path)
    return remove_json(path, client.fmt)


def is_connected(key: str) -> bool:
    client = CLIENTS[key]
    path = client.path()
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if client.fmt == "toml":
        return _strip_table(text)[1]
    try:
        cfg = json.loads(text or "{}")
    except ValueError:
        return False
    key_name = "servers" if client.fmt == "vscode" else "mcpServers"
    return isinstance(cfg, dict) and SERVER_NAME in (cfg.get(key_name) or {})
