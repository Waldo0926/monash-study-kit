"""入口和路径模块共用的目录规则；不初始化任何账号路径。"""
from __future__ import annotations

import json
import os
import re
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


DEFAULT_HOME = _default_home()
LOCATION_FILE = DEFAULT_HOME / "location.json"   # 数据放在别处时，这里记着实际位置


def resolve_home(environ=os.environ, location_file: Path = LOCATION_FILE, default: Path = DEFAULT_HOME) -> Path:
    """MONASH_KIT_HOME > location.json 里记的位置 > 系统默认位置。location.json 坏了或写的不是绝对路径就当没有。"""
    if environ.get("MONASH_KIT_HOME"):
        return Path(environ["MONASH_KIT_HOME"])
    try:
        home = Path(json.loads(location_file.read_text(encoding="utf-8"))["home"]).expanduser()
    except (OSError, ValueError, KeyError, TypeError):
        return default
    return home if home.is_absolute() else default



def validate_profile(name: str) -> str:
    """default 保留给默认账号，命名 profile 不能使用它。"""
    if name == "default" or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
        raise ValueError("profile 名称只能是 1–32 位小写字母、数字、下划线或连字符，首位须为字母或数字；default 是保留名")
    return name
