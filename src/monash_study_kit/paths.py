"""所有东西放在哪：一个目录装下数据、课件、登录凭据和专用浏览器配置。

    macOS    ~/Library/Application Support/monash-study-kit
    Windows  %LOCALAPPDATA%\\monash-study-kit
    Linux    ~/.local/share/monash-study-kit

设 MONASH_KIT_HOME 可以换位置（测试也靠它）。卸载时删掉这个目录就干净了。
用户可改的设置在 settings.json（`monash config`）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "monash-study-kit"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "monash-study-kit"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "monash-study-kit"


HOME = Path(os.environ.get("MONASH_KIT_HOME") or _default_home())
SETTINGS_FILE = HOME / "settings.json"

DEFAULTS = {
    "download_videos": False,     # 录像动辄几百 MB；没装 [media] 转不了字幕，下下来 Claude 也看不了
    "auto_sync_hours": 1,         # MCP 在跑的时候，多久在后台同步一次
    "keepalive_minutes": 20,      # MCP 在跑的时候，多久给 Moodle 会话续一次期（空闲 4 小时就过期）
    "tz_offset": 8,               # Moodle 账号的时区（页面上的时间按它显示）：马来西亚 8，澳洲 10（夏令时 11）
    "files_dir": "",              # 课件放哪；空 = 数据目录下的 files。Windows 路径太长时改成 C:\\Monash 之类
    "browser": "",                # 登录窗口用哪个浏览器程序；空 = 自动找 Chrome / Edge / Brave
}


def load_settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


# 设置在导入时读一次：Claude Desktop 启动的 MCP 读不到终端里设的环境变量，所以常用的都进 settings.json；
# 环境变量仍然优先（测试和高级用法）。
_s = load_settings()
SECRETS_DIR = HOME / "secrets"          # Moodle cookie、Ed 令牌、日历链接（600 权限）
DATA_DIR = HOME / "data"                # moodle.db、ed.db
FILES_DIR = Path(os.environ.get("MONASH_KIT_FILES") or _s["files_dir"] or HOME / "files").expanduser()
ED_FILES_DIR = DATA_DIR / "ed-files"    # Ed Lessons 的课件 PDF
BROWSER_PROFILE = HOME / "browser-profile"   # 专用登录窗口的配置目录，和日常浏览器分开
LOG_FILE = HOME / "mcp.log"
TZ_OFFSET = float(os.environ.get("MONASH_KIT_TZ_OFFSET") or _s["tz_offset"])
BROWSER = os.environ.get("MONASH_KIT_BROWSER") or _s["browser"]


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_secret(path: Path, text: str) -> None:
    """原子写入、只有自己能读（Windows 上 chmod 不起作用，但目录本来就在用户自己的目录下）。"""
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save_settings(values: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    cur = {}
    try:
        cur = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    cur.update(values)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)
