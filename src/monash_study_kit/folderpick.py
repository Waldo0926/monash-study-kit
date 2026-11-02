"""弹出系统自带的“选择文件夹”窗口（导出课程资料前选保存位置）。

macOS 用 osascript，Windows 用 PowerShell 的 FolderBrowserDialog，Linux 用 zenity / kdialog。
都不用额外装东西；弹不出来（SSH、没有桌面）时抛 Unavailable，由调用方改用默认位置。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT = 600  # 窗口开着没人管，十分钟后放弃

_MAC = [
    "on run argv",
    "activate",
    "return POSIX path of (choose folder with prompt (item 1 of argv) default location (POSIX file (item 2 of argv)))",
    "end run",
]

# 提示文字和默认位置走环境变量传进去，命令本身不拼接任何路径
_WINDOWS = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
    "$d.Description = $env:MONASH_PICK_PROMPT;"
    "$d.SelectedPath = $env:MONASH_PICK_DEFAULT;"
    "$d.ShowNewFolderButton = $true;"
    "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true};"
    "if ($d.ShowDialog($owner) -eq 'OK') { $d.SelectedPath }"
)


def _system() -> str:
    """mac / windows / linux；单独一个函数，测试里只换它，不动全局的 os.name。"""
    if sys.platform == "darwin":
        return "mac"
    return "windows" if sys.platform == "win32" else "linux"


class Unavailable(Exception):
    """这台机器上弹不出选择窗口。"""


def _run(argv: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, timeout=TIMEOUT, env=env,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(str(e)) from None


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip()


def choose_folder(prompt: str, initial: Path) -> Path | None:
    """返回选中的文件夹；用户点了取消返回 None；弹不出来抛 Unavailable。"""
    system = _system()
    if system == "mac":
        argv = ["osascript"]
        for line in _MAC:
            argv += ["-e", line]
        r = _run(argv + [prompt, str(initial)])
        if r.returncode != 0:
            if b"-128" in r.stderr:  # 用户点了取消
                return None
            raise Unavailable(_text(r.stderr))
        out = _text(r.stdout)
    elif system == "windows":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            raise Unavailable("找不到 PowerShell")
        env = {**os.environ, "MONASH_PICK_PROMPT": prompt, "MONASH_PICK_DEFAULT": str(initial)}
        r = _run([exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", _WINDOWS], env=env)
        if r.returncode != 0:
            raise Unavailable(_text(r.stderr))
        out = _text(r.stdout)
    else:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise Unavailable("没有图形界面")
        if shutil.which("zenity"):
            argv = ["zenity", "--file-selection", "--directory", f"--title={prompt}",
                    f"--filename={initial}{os.sep}"]
        elif shutil.which("kdialog"):
            argv = ["kdialog", "--getexistingdirectory", str(initial), "--title", prompt]
        else:
            raise Unavailable("没有 zenity 或 kdialog")
        r = _run(argv)
        if r.returncode == 1:  # 两者都用 1 表示取消
            return None
        if r.returncode != 0:
            raise Unavailable(_text(r.stderr))
        out = _text(r.stdout)
    return Path(out) if out else None
