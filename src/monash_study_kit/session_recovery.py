"""后台只尝试无界面恢复；MFA 和可见登录窗口仍由用户主动发起。"""
from __future__ import annotations

import threading
import time

from . import jobs
from .moodlelib import now_iso, MoodleError
from .paths import BROWSER_PROFILE, load_settings
from .local_state import file_lock

_guard = threading.Lock()


def start(log) -> bool:
    settings = load_settings()
    if settings.get("materials_only") or not settings.get("auto_relogin", True):
        return False
    if not _guard.acquire(blocking=False):
        return False
    try:
        threading.Thread(target=_recover, args=(log,), name="session-recovery", daemon=True).start()
    except Exception:
        _guard.release()
        raise
    return True


def _recover(log) -> None:
    try:
        try:
            with file_lock(BROWSER_PROFILE.parent / "session-recovery.lock", timeout=0):
                old = jobs.read_status().get("session", {})
                if time.time() < old.get("next_recovery_at", 0):
                    return
                state = {"alive": False, "state": "recovering", "checked_at": now_iso(),
                         "next_recovery_at": time.time() + 900}
                jobs.update_status({"session": state})
                if not (BROWSER_PROFILE / "Default").exists():
                    jobs.update_status({"session": {**state, "state": "needs_login"}})
                    return
                from .browser_login import ensure_login, LoginFailed
                try:
                    ensure_login(log=log, allow_window=False)
                except LoginFailed:
                    jobs.update_status({"session": {**state, "state": "needs_login"}})
                    log("Moodle 需要重新登录，请运行 monash login 或主动打开登录工具")
                except (OSError, TimeoutError, MoodleError):
                    jobs.update_status({"session": {**state, "state": "network_error",
                                                    "next_recovery_at": time.time() + 60}})
                else:
                    jobs.keepalive()
                    from .mcp_server import start_sync
                    start_sync()
        except TimeoutError:
            pass  # 另一个客户端已经在恢复
    except Exception as e:  # noqa: BLE001  保留后台线程，不把异常中的凭据写入状态
        log(f"无界面会话恢复失败：{type(e).__name__}")
    finally:
        _guard.release()
