"""把 monash MCP 加进 Claude Desktop 和 Claude Code。

Claude Desktop 的坑：App 运行时把配置文件攥在内存里，退出时整份写回。开着 App 改
claude_desktop_config.json，一退出就被覆盖掉。所以必须先完全退出 App（macOS 上我们自动退，
Windows 上请用户从托盘图标退出），确认进程没了再改。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVER_NAME = "monash"


def server_command() -> list[str]:
    """启动 MCP 的命令：用装好的 monash 程序的绝对路径，Claude 启动它时不依赖 PATH。"""
    exe = shutil.which("monash")
    if exe:
        return [str(Path(exe).resolve()), "mcp"]
    return [sys.executable, "-m", "monash_study_kit", "mcp"]


# ---------------------------------------------------------------- Claude Desktop

def desktop_config_candidates() -> list[Path]:
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
    if os.name == "nt":
        out = []
        if os.environ.get("APPDATA"):
            out.append(Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json")
        # 从 Microsoft Store / MSIX 装的版本，配置在包的虚拟化目录里
        pkgs = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
        if pkgs.exists():
            for p in pkgs.glob("Claude_*"):
                out.append(p / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
        return out
    return [Path.home() / ".config" / "Claude" / "claude_desktop_config.json"]


def desktop_config_path() -> Path | None:
    cands = desktop_config_candidates()
    for p in cands:
        if p.exists() or p.parent.exists():
            return p
    return None


def desktop_running() -> bool:
    if sys.platform == "darwin":
        return subprocess.run(["pgrep", "-xq", "Claude"]).returncode == 0
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe", "/NH"], capture_output=True, text=True)
        return "claude.exe" in r.stdout.lower()
    return subprocess.run(["pgrep", "-x", "claude-desktop"], capture_output=True).returncode == 0


def quit_desktop(wait: int = 60) -> bool:
    """退出 Claude Desktop 并等进程结束。返回是否已经没在运行。"""
    if not desktop_running():
        return True
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'quit app "Claude"'], capture_output=True)
    else:
        return False        # Windows 上 Claude 会缩到托盘，让用户自己退出更可靠
    for _ in range(wait):
        if not desktop_running():
            return True
        time.sleep(1)
    return False


def write_desktop_config(path: Path, command: list[str]) -> Path | None:
    """加上（或更新）mcpServers.monash，其他内容原样保留。返回备份文件路径。"""
    cfg, backup = {}, None
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        cfg = json.loads(raw) if raw.strip() else {}
        backup = path.with_name(path.name + f".bak-{time.strftime('%Y%m%d%H%M%S')}")
        backup.write_text(raw, encoding="utf-8")
    cfg.setdefault("mcpServers", {})[SERVER_NAME] = {"command": command[0], "args": command[1:]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def remove_from_desktop_config(path: Path) -> bool:
    if not path.exists():
        return False
    cfg = json.loads(path.read_text(encoding="utf-8") or "{}")
    if SERVER_NAME not in cfg.get("mcpServers", {}):
        return False
    del cfg["mcpServers"][SERVER_NAME]
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def open_desktop() -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Claude"], capture_output=True)


def install_desktop(ask=input, log=print) -> bool:
    path = desktop_config_path()
    if path is None:
        log("没找到 Claude Desktop（没装，或者从没打开过）。装好并打开一次后再运行 monash setup claude")
        return False
    if desktop_running():
        if sys.platform == "darwin":
            if ask("要先退出 Claude Desktop 才能改它的配置（开着改会被覆盖）。现在退出吗？[Y/n] ").strip().lower() not in ("", "y", "yes"):
                log("跳过了 Claude Desktop。之后可以运行 monash setup claude")
                return False
            if not quit_desktop():
                log("Claude 60 秒内没退出。手动按 ⌘Q 退出后再运行 monash setup claude")
                return False
        else:
            log("请完全退出 Claude Desktop：右下角托盘里的 Claude 图标 → 右键 → Quit（只关窗口不够）。")
            for _ in range(3):
                ask("退出后按回车继续…")
                if not desktop_running():
                    break
            else:
                log("Claude 还在运行，先跳过。退出后再运行 monash setup claude")
                return False
    backup = write_desktop_config(path, server_command())
    log(f"已把 monash 加进 Claude Desktop（{path}）" + (f"，原配置备份在 {backup.name}" if backup else ""))
    open_desktop()
    log("重新打开 Claude Desktop 后，在聊天输入框的“+ → Connectors”里能看到 monash。")
    return True


# ---------------------------------------------------------------- Claude Code

def install_code(log=print) -> bool:
    claude = shutil.which("claude")
    if not claude:
        return False
    cmd = server_command()
    subprocess.run([claude, "mcp", "remove", "-s", "user", SERVER_NAME], capture_output=True)
    r = subprocess.run([claude, "mcp", "add", "-s", "user", SERVER_NAME, "--", *cmd], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"加进 Claude Code 失败：{(r.stderr or r.stdout).strip()}")
        return False
    log("已把 monash 加进 Claude Code（所有项目都能用）")
    return True
