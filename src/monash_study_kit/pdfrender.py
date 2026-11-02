"""用临时、无登录状态的 Chromium 打印 HTML；只接收已经内嵌图片的离线页面。"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .browser_login import find_browser as _find_browser
from .cdp import CDP, PipeCDP
from .paths import DATA_DIR, ensure_private_dir

CACHE_LIMIT = 500 * 1024 * 1024
NO_BROWSER = "没有找到 Chrome / Edge，PDF 没有生成（装一个再导出即可）"


def find_browser() -> str | None:
    executable = _find_browser()
    # browser 设置可能指向卸载后的程序，此时也按未安装处理。
    return shutil.which(str(Path(executable).expanduser())) if executable else None


class Renderer:
    """一次导出共用一个浏览器。POSIX 用管道；Windows 沿用项目的随机本机端口方案。"""

    def __init__(self, executable: str):
        self.executable = executable
        self.temp = None
        self.cdp = self.proc = None

    def __enter__(self):
        self.temp = tempfile.mkdtemp(prefix="monash-pdf-")
        self.root = Path(self.temp)
        flags = [self.executable, f"--user-data-dir={self.root / 'profile'}", "--headless=new",
                 "--no-first-run", "--no-default-browser-check", "--disable-sync",
                 "--disable-background-networking", "--disable-component-update",
                 "--disable-domain-reliability", "--disable-extensions", "--disable-default-apps",
                 "--disable-client-side-phishing-detection", "--metrics-recording-only",
                 "--safebrowsing-disable-auto-update", "--no-pings",
                 "--host-resolver-rules=MAP * ~NOTFOUND",
                 "--proxy-server=http://127.0.0.1:0", "--proxy-bypass-list=<-loopback>", "about:blank"]
        try:
            if os.name != "nt":
                self.cdp = PipeCDP([*flags, "--remote-debugging-pipe"])
                self.proc = self.cdp.proc
                self.cdp.call("Browser.getVersion", timeout=30)
            else:
                self.proc = subprocess.Popen([*flags, "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"],
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                port_file = self.root / "profile" / "DevToolsActivePort"
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if port_file.is_file():
                        lines = port_file.read_text(encoding="utf-8").splitlines()
                        if len(lines) >= 2:
                            self.cdp = CDP(f"ws://127.0.0.1:{int(lines[0])}{lines[1]}")
                            break
                    if self.proc.poll() is not None:
                        break
                    time.sleep(.1)
                if self.cdp is None:
                    raise RuntimeError("PDF 浏览器没能启动")
            target = self.cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
            self.session = self.cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            self.call("Page.enable")
            self.call("Network.enable")
            self.call("Network.setBlockedURLs", {"urls": ["http://*", "https://*", "ws://*", "wss://*", "ftp://*"]})
            self.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0,
                                                          "downloadThroughput": 0, "uploadThroughput": 0})
            return self
        except BaseException:
            self.close()
            raise

    def call(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        return self.cdp.call(method, params, timeout=timeout, session_id=self.session)

    def render(self, html: str) -> bytes:
        page = self.root / "page.html"
        page.write_text(html, encoding="utf-8")
        # 每页换 URL，避免重复导航时读到上一页的 readyState。
        result = self.call("Page.navigate", {"url": page.as_uri() + f"?{hashlib.sha256(html.encode()).hexdigest()}"})
        if result.get("errorText"):
            raise RuntimeError("PDF 页面加载失败：" + result["errorText"])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.call("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True}).get("result", {}).get("value") == "complete":
                break
            time.sleep(.05)
        else:
            raise TimeoutError("PDF 页面加载超时")
        ready = self.call("Runtime.evaluate", {"expression": "Promise.all([document.fonts.ready, ...Array.from(document.images, i => i.decode().catch(() => {}))]).then(() => true)",
                                               "awaitPromise": True, "returnByValue": True})
        if ready.get("exceptionDetails"):
            raise RuntimeError("PDF 页面图片或字体准备失败")
        result = self.call("Page.printToPDF", {"paperWidth": 210 / 25.4, "paperHeight": 297 / 25.4,
                                              "marginTop": 15 / 25.4, "marginBottom": 15 / 25.4,
                                              "marginLeft": 15 / 25.4, "marginRight": 15 / 25.4,
                                              "printBackground": True, "preferCSSPageSize": True})
        data = base64.b64decode(result["data"], validate=True)
        if not data.startswith(b"%PDF"):
            raise RuntimeError("浏览器没有生成有效 PDF")
        return data

    def close(self):
        try:
            if self.cdp:
                try:
                    self.cdp.call("Browser.close", timeout=3)
                except (OSError, RuntimeError, TimeoutError):
                    pass
                finally:
                    self.cdp.close()
        finally:
            try:
                if self.proc:
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=5)
            finally:
                if self.temp:
                    _remove_tree(self.temp)
                    self.temp = None

    def __exit__(self, *exc):
        self.close()


def _remove_tree(path: str, tries: int = 20) -> None:
    """删临时浏览器目录。Windows 上 Chrome 主进程退出后，子进程还会占着文件一会儿，
    所以重试几秒；还删不掉就留给系统清理，不能因此让导出失败。"""
    for _ in range(tries):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.25)
    shutil.rmtree(path, ignore_errors=True)


def cache_dir() -> Path:
    return DATA_DIR / "pdf-cache"


def cached(html: str) -> bytes | None:
    path = cache_dir() / (hashlib.sha256(html.encode("utf-8")).hexdigest() + ".pdf")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return data if data.startswith(b"%PDF") else None


def store(html: str, data: bytes) -> None:
    root = cache_dir()
    ensure_private_dir(root)
    path = root / (hashlib.sha256(html.encode("utf-8")).hexdigest() + ".pdf")
    # 并行导出不共用 .tmp 文件；replace 后读者只会看到完整的 PDF。
    fd, tmp = tempfile.mkstemp(dir=root, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    prune_cache()


def prune_cache() -> None:
    files = []
    for p in cache_dir().glob("*.pdf"):
        try:
            files.append((p.stat().st_mtime_ns, p, p.stat().st_size))
        except FileNotFoundError:
            pass
    total = sum(size for _, _, size in files)
    for _, path, size in sorted(files):
        if total <= CACHE_LIMIT:
            break
        path.unlink(missing_ok=True)
        total -= size
