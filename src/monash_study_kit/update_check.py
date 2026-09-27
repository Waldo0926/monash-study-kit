"""新版本提示：每天最多查一次 GitHub 上 main 分支的版本号，结果缓存在本地。

朋友不会主动运行 monash update，修了 bug 也拿不到，所以在他们会看到的地方提一句：
命令行（stderr 一行）、Claude 里的 study_todo / monash_status、monash doctor。

  * 命令行只读缓存，绝不等网络；缓存过期就在后台线程里刷新，下一次运行才看得到。
  * 查的是 raw.githubusercontent.com 上的 __init__.py，不带任何身份信息。
  * 关掉：monash config update_check false，或者设 MONASH_KIT_NO_UPDATE_CHECK=1（CI 里自动关）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request

from . import __version__
from .paths import HOME, load_settings

VERSION_URL = "https://raw.githubusercontent.com/Waldo0926/monash-study-kit/main/src/monash_study_kit/__init__.py"
CACHE_FILE = HOME / "update-check.json"
EVERY = 24 * 3600
VERSION_RE = re.compile(r'__version__\s*=\s*"([0-9][0-9A-Za-z.\-+]*)"')


def parse(v: str) -> tuple:
    """"0.10.2" → (0, 10, 2)；认不出来的部分当 0，免得一个怪版本号让比较崩掉。"""
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", v or "0"))


def is_newer(latest: str | None, current: str = __version__) -> bool:
    return bool(latest) and parse(latest) > parse(current)


def enabled() -> bool:
    if os.environ.get("MONASH_KIT_NO_UPDATE_CHECK") or os.environ.get("CI"):
        return False
    return bool(load_settings().get("update_check", True))


def read_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fetch_latest(timeout: float = 5) -> str | None:
    req = urllib.request.Request(VERSION_URL, headers={"User-Agent": f"monash-study-kit/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        m = VERSION_RE.search(r.read(4096).decode("utf-8", "replace"))
    return m.group(1) if m else None


def refresh(force: bool = False) -> dict:
    """需要的话查一次，写缓存。网络出错就记下时间、下次再说，绝不抛出去。"""
    cache = read_cache()
    if not force and time.time() - cache.get("checked_at", 0) < EVERY:
        return cache
    try:
        latest = fetch_latest()
    except Exception:  # noqa: BLE001 —— 没网、GitHub 抽风：安静地跳过
        latest = cache.get("latest")
    cache = {"checked_at": time.time(), "latest": latest}
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass
    return cache


def refresh_in_background() -> None:
    if enabled() and time.time() - read_cache().get("checked_at", 0) >= EVERY:
        threading.Thread(target=refresh, name="update-check", daemon=True).start()


def available() -> str | None:
    """缓存里有比当前新的版本就返回它（只读缓存，不联网）。"""
    if not enabled():
        return None
    latest = read_cache().get("latest")
    return latest if is_newer(latest) else None


def notice() -> str | None:
    latest = available()
    if not latest:
        return None
    return f"monash 有新版本 {latest}（现在是 {__version__}）：运行 monash update 更新，然后重启 Claude Desktop"


def clear() -> None:
    try:
        CACHE_FILE.unlink()
    except OSError:
        pass
