"""在 AI 软件里一句话接上学校邮箱或考勤，不用开终端。

用户说“帮我接上学校邮箱”时，AI 调用 start_school_service_setup，这里在后台按顺序做：

  1. 缺的 Python 组件自动装进当前环境（和 monash-study-kit[gmail] / [attendance] 一样的那几个）；
  2. 考勤还要装 playwright 的浏览器内核（一次，约 150 MB）；
  3. 在用户电脑上打开登录窗口：Gmail 用安装包自带的 Google 应用，考勤打开门户；
  4. 登录成功后打开对应设置，马上同步一次。

登录只在用户自己的浏览器里完成，密码和验证码不经过 AI。同一时间只做一件事；
再调用一次 status() 可以看到做到哪一步了。
"""
from __future__ import annotations

import importlib
import importlib.util
import shutil
import subprocess
import sys
import threading
from pathlib import Path

# 要和 pyproject.toml 里的 optional-dependencies 保持一致，tests/test_service_setup.py 会对照
PACKAGES = {
    "gmail": ["google-auth-oauthlib>=1.2,<2"],
    "attendance": ["playwright>=1.49,<2", "Pillow>=10,<13", "truststore>=0.10,<1"]
    + (["tzdata"] if sys.platform == "win32" else []),
}
MODULES = {"gmail": ["google_auth_oauthlib"], "attendance": ["playwright", "PIL", "truststore"]}
NAMES = {"gmail": "学校邮箱", "attendance": "考勤"}

_state: dict = {"service": None, "step": None, "running": False, "result": None, "error": None}
_lock = threading.Lock()


def missing(service: str) -> list[str]:
    importlib.invalidate_caches()
    return [m for m in MODULES[service] if importlib.util.find_spec(m) is None]


def _uv() -> str | None:
    # 从 Claude Desktop 这类图形界面启动时 PATH 很短，常见安装位置也找一下
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    for candidate in (home / ".local" / "bin" / "uv", home / ".cargo" / "bin" / "uv",
                      home / ".local" / "bin" / "uv.exe", home / ".cargo" / "bin" / "uv.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def install_command(packages: list[str]) -> list[str]:
    uv = _uv()
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, *packages]
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", *packages]
    raise RuntimeError("没找到 uv 或 pip，没法自动安装。请在终端运行："
                       'uv tool install --reinstall "monash-study-kit[gmail,attendance]"')


def _run(command: list[str], timeout: int) -> None:
    done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError("安装失败：" + " / ".join(tail))


def _step(text: str) -> None:
    _state["step"] = text


def _prepare(service: str) -> None:
    if missing(service):
        _step(f"正在安装{NAMES[service]}需要的组件（一两分钟）")
        _run(install_command(PACKAGES[service]), timeout=900)
        if missing(service):
            raise RuntimeError("组件装好了但这个进程还读不到，请重启 AI 软件后再说一次")
    if service == "attendance":
        _step("正在下载考勤用的浏览器内核（第一次约 150 MB，几分钟）")
        _run([sys.executable, "-m", "playwright", "install", "chromium"], timeout=1800)


def _connect(service: str, read_only: bool, after) -> None:
    from .paths import save_settings
    try:
        _prepare(service)
        if service == "gmail":
            from . import gmail
            from .cli_gmail import BUNDLED_CLIENT
            if not BUNDLED_CLIENT.is_file():
                raise RuntimeError("这个版本没有自带的 Google 应用，请按教程在终端用 monash gmail login --client 登录")
            _step("已在浏览器里打开 Google 授权页，请用户选学校邮箱并完成 Okta 登录")
            result = gmail.login(BUNDLED_CLIENT, manage=not read_only)
            save_settings({"gmail_enabled": True, "gmail_manage_enabled": not read_only})
        else:
            from . import attendance
            _step("已在用户电脑上打开考勤门户的登录窗口，请用户在里面完成 Okta 登录")
            result = attendance.login()
            save_settings({"attendance_enabled": True, "attendance_submit_enabled": True})
        _state.update(result={"connected": NAMES[service], **result}, step="已接好，正在做第一次同步")
        if after:
            after(service)
        _step("完成")
    except Exception as e:  # noqa: BLE001  任何一步失败都要告诉用户，而不是让线程悄悄死掉
        _state.update(error=f"{type(e).__name__}: {e}"[:400], step="失败")
    finally:
        _state["running"] = False


def start(service: str, read_only: bool = False, after=None) -> dict:
    """开始接入；已经在做就返回当前进度。after(service) 在登录成功后调用（同步、刷新工具列表）。"""
    if service not in NAMES:
        raise ValueError("service 只能是 gmail 或 attendance")
    with _lock:
        if _state["running"]:
            return status()
        _state.update(service=service, step="开始", running=True, result=None, error=None)
        threading.Thread(target=_connect, args=(service, read_only, after), name="connect-service",
                         daemon=True).start()
    return status()


def status() -> dict:
    return {k: v for k, v in _state.items() if v is not None or k in ("running",)}


def already_connected(service: str, settings: dict) -> bool:
    if service == "gmail":
        from .paths import SECRETS_DIR
        return bool(settings.get("gmail_enabled")) and (SECRETS_DIR / "gmail-token.json").is_file() \
            and not missing("gmail")
    from .paths import SECRETS_DIR
    return bool(settings.get("attendance_enabled")) and (SECRETS_DIR / "attendance-state.json").is_file() \
        and not missing("attendance")

