"""所有东西放在哪：一个目录装下数据、课件、登录凭据和专用浏览器配置。

    macOS    ~/Library/Application Support/monash-study-kit
    Windows  %LOCALAPPDATA%\\monash-study-kit
    Linux    ~/.local/share/monash-study-kit

设 MONASH_KIT_HOME 可以换位置（测试也靠它）。卸载时删掉这个目录就干净了。
用户可改的设置在 settings.json（`monash config`）。

服务器上的部署把数据、凭据、Ed 库放在各自的目录，用这几个环境变量指过去：
    MONASH_KIT_DATA     moodle.db 所在目录
    MONASH_KIT_SECRETS  cookie、日历链接、Ed 令牌所在目录
    MONASH_KIT_FILES    课件目录
    MONASH_KIT_ED_DATA  Ed 的数据目录（里面是 ed.db 和 files/，ed-digest 就是这个布局）
    MONASH_KIT_ED_TOKEN Ed 令牌文件
settings.json 里的每一项也能用 MONASH_KIT_<大写名字> 覆盖，比如 MONASH_KIT_DOWNLOAD_VIDEOS=true。
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
    "web_notes": True,            # 抓 Moodle / Ed 链接到的课程笔记网页（老师的 github.io 讲义站之类）进全文搜索
    "update_check": True,         # 每天查一次 GitHub 上有没有新版本，有就提示
    "name_limit": 80,             # 文件名最长几个字符：Windows 整条路径不能超过 260，课程/周次/文件夹/文件四层很容易超
    "pdf_extractor": "pypdf",     # PDF 抽文字用什么：pypdf（随包装好）或 pdftotext（要另装 poppler，排版更准）
}


def _env_value(key: str, default):
    """MONASH_KIT_<KEY> 覆盖一项设置，按默认值的类型转换。"""
    raw = os.environ.get("MONASH_KIT_" + key.upper())
    if raw is None:
        return None
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, (int, float)):
        try:
            return type(default)(float(raw))
        except ValueError:
            return None
    return raw


def load_settings() -> dict:
    try:
        out = {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        v = _env_value(key, default)
        if v is not None:
            out[key] = v
    return out


# 设置在导入时读一次：Claude Desktop 启动的 MCP 读不到终端里设的环境变量，所以常用的都进 settings.json；
# 环境变量仍然优先（测试和高级用法）。
_s = load_settings()
SECRETS_DIR = Path(os.environ.get("MONASH_KIT_SECRETS") or HOME / "secrets")   # Moodle cookie、Ed 令牌、日历链接（600 权限）
DATA_DIR = Path(os.environ.get("MONASH_KIT_DATA") or HOME / "data")            # moodle.db、ed.db
FILES_DIR = Path(os.environ.get("MONASH_KIT_FILES") or _s["files_dir"] or HOME / "files").expanduser()
_ED_DATA = os.environ.get("MONASH_KIT_ED_DATA")
ED_DB = Path(_ED_DATA) / "ed.db" if _ED_DATA else DATA_DIR / "ed.db"
ED_FILES_DIR = Path(_ED_DATA) / "files" if _ED_DATA else DATA_DIR / "ed-files"   # Ed Lessons 的课件 PDF
ED_TOKEN_FILE = Path(os.environ.get("MONASH_KIT_ED_TOKEN") or SECRETS_DIR / "ed-token")
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
    # 临时文件名带进程号：Claude Desktop 和 Claude Code 各开一个 MCP，后台同步也会续 cookie，
    # 共用一个 .tmp 会互相覆盖，后 replace 的那个直接报错
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
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
