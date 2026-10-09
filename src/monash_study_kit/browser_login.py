"""专用登录窗口：用单独的浏览器配置目录打开 Moodle，登录完只从这个窗口里取 Moodle 的 cookie。

为什么这样做：
  * Monash 走 Okta SAML + MFA，命令行没法自己登录，只能让人在真浏览器里登。
  * 不去读日常 Chrome 的 cookie：那要钥匙串里的解密密钥，能解开所有网站的登录状态。
    这里用的是 BROWSER_PROFILE 这个独立目录，里面只有你在这个窗口里登录过的东西。
  * 独立目录会记住 Okta 的“保持登录”，所以下次过期时可以先在后台（无界面）试着自动登录，
    Okta 那边还认你的话就不用再弹窗；不认才打开窗口让你登。

浏览器：Chrome、Edge、Brave、Chromium 都行（Windows 自带 Edge）。`monash config browser <路径>` 可以指定。
Chrome 136 起不许对默认配置目录开远程调试，独立目录不受影响。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from .cdp import CDP, WebSocketClosed
from .moodlelib import BASE_URL, HOST, MoodleAuthError, MoodleClient, save_cookies
from .paths import BROWSER, BROWSER_PROFILE, ensure_private_dir

KEEP = ("MoodleSession", "MOODLEID", "AWSALB", "AWSELB")   # 只留 Moodle 自己要的，别存一堆统计 cookie


class LoginFailed(RuntimeError):
    pass


def find_browser() -> str | None:
    if BROWSER:
        return BROWSER
    if sys.platform == "darwin":
        for app, exe in (("Google Chrome", "Google Chrome"), ("Microsoft Edge", "Microsoft Edge"),
                         ("Brave Browser", "Brave Browser"), ("Chromium", "Chromium")):
            for root in (Path("/Applications"), Path.home() / "Applications"):
                p = root / f"{app}.app" / "Contents" / "MacOS" / exe
                if p.exists():
                    return str(p)
        return None
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                    r"BraveSoftware\Brave-Browser\Application\brave.exe"):
            for root in filter(None, roots):
                p = Path(root) / rel
                if p.exists():
                    return str(p)
        return None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "microsoft-edge", "brave-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


class Browser:
    """一个用 BROWSER_PROFILE 启动、开着远程调试端口的浏览器。"""

    def __init__(self, url: str, headless: bool = False):
        exe = find_browser()
        if not exe:
            raise LoginFailed("没找到 Chrome / Edge / Brave。装一个 Chrome，或者用 monash config browser <路径> 指定浏览器程序")
        ensure_private_dir(BROWSER_PROFILE)
        port_file = BROWSER_PROFILE / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        args = [exe, f"--user-data-dir={BROWSER_PROFILE}", "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check", "--disable-sync",
                "--window-size=1100,860", "--new-window", url]
        if headless:
            args.insert(1, "--headless=new")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if port_file.exists():
                lines = port_file.read_text(encoding="utf-8").split()
                if len(lines) >= 2:
                    self.cdp = CDP(f"ws://127.0.0.1:{lines[0]}{lines[1]}")
                    return
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        self.proc.kill()
        raise LoginFailed("浏览器没能启动（如果这个专用窗口已经开着，先把它关掉再试）")

    def cookies(self) -> dict[str, str]:
        got = self.cdp.call("Storage.getCookies").get("cookies", [])
        return {c["name"]: c["value"] for c in got
                if _domain_matches(c.get("domain", "")) and c["name"].startswith(KEEP)}

    def page_urls(self) -> list[str]:
        infos = self.cdp.call("Target.getTargets").get("targetInfos", [])
        return [t.get("url", "") for t in infos if t.get("type") == "page"]

    def close(self) -> None:
        try:
            self.cdp.call("Browser.close", timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.cdp.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _domain_matches(domain: str) -> bool:
    """cookie 的 domain 是 Moodle 本站或它的上级域名。按点分段比，免得 "ash.edu" 也算进 monash.edu。"""
    d = domain.lstrip(".").lower()
    return bool(d) and (HOST == d or HOST.endswith("." + d))


def _on_moodle(urls: list[str]) -> bool:
    """有页面停在 Moodle 本站、而且不是登录页，大概登录完了，值得验证一下。"""
    for u in urls:
        p = urllib.parse.urlsplit(u)
        if p.hostname == HOST and not p.path.startswith(("/login", "/auth")):
            return True
    return False


def _on_okta(urls: list[str]) -> bool:
    return any((urllib.parse.urlsplit(u).hostname or "").endswith("okta.com") for u in urls)


def login_moodle(interactive: bool = True, timeout: float = 300, log=print) -> dict:
    """登录 Moodle 并保存 cookie。返回 {"userid", "time_remaining"}。

    interactive=False：无界面试一次，靠专用目录里记住的 Okta 登录状态自动登进去；停在 Okta
    登录页超过 20 秒就算失败（调用方再决定要不要弹窗）。
    """
    browser = Browser(f"{BASE_URL}/my/", headless=not interactive)
    if interactive:
        log("已打开登录窗口：在里面登录 Monash（Okta + MFA），登录成功后窗口会自动关闭。")
    started = time.monotonic()
    tried: dict[str, float] = {}     # 会话值 → 上次验证的时间；登录前的匿名会话也叫 MoodleSession
    try:
        while time.monotonic() - started < timeout:
            time.sleep(1.5)
            try:
                urls = browser.page_urls()
                jar = browser.cookies()
            except (WebSocketClosed, OSError):
                raise LoginFailed("登录窗口被关掉了，没有完成登录") from None
            if not interactive and _on_okta(urls) and time.monotonic() - started > 20:
                raise LoginFailed("自动登录不行（Okta 要重新输入密码或 MFA）")
            session = jar.get("MoodleSession")
            if not session or not _on_moodle(urls) or time.monotonic() - tried.get(session, -99) < 6:
                continue
            tried[session] = time.monotonic()
            client = MoodleClient(dict(jar), persist=False)
            try:
                left = client.time_remaining()
            except MoodleAuthError:
                continue
            save_cookies(client.jar)
            _save_calendar_url(client, log)
            return {"userid": client.userid, "time_remaining": left}
        raise LoginFailed("等了太久还没登录成功" if interactive else "自动登录超时")
    finally:
        browser.close()


def _save_calendar_url(client: MoodleClient, log) -> None:
    """顺手存下 iCal 订阅链接：以后会话过期了，截止日期还能靠它拿到。"""
    from . import features as F
    if F.load_calendar_url():
        return
    try:
        F.calendar_url(client)
    except Exception as e:  # noqa: BLE001
        log(f"（日历订阅链接没拿到：{e}）")


def ensure_login(log=print, allow_window: bool = True) -> dict:
    """先无界面试自动登录（专用窗口以前登录过才试），不行再开窗口。"""
    if (BROWSER_PROFILE / "Default").exists():
        try:
            return login_moodle(interactive=False, timeout=45, log=log)
        except LoginFailed as e:
            if not allow_window:
                raise
            log(f"{e}，打开登录窗口…")
    elif not allow_window:
        raise LoginFailed("还没在登录窗口里登录过，没法自动登录")
    return login_moodle(interactive=True, log=log)


def session_ok() -> bool:
    try:
        MoodleClient().time_remaining()
        return True
    except (MoodleAuthError, OSError):
        return False

