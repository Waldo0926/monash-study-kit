"""monash doctor：把常见问题一项项查一遍，每项给出“怎么修”。

输出是给朋友发给帮忙的人看的，所以绝不打印令牌、cookie、日历链接、邮箱；
路径、版本、课号、报错摘要可以打。每项检查互相独立，一项出错不影响别的。
"""
from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import urllib.error
from datetime import datetime, timezone

from . import __version__, claude_setup, edlib, jobs
from .moodlelib import MoodleAuthError, MoodleClient, MoodleError, db_connect
from .paths import DEFAULTS, FILES_DIR, HOME, LOG_FILE, load_settings

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "✓", WARN: "!", FAIL: "✗"}
ERROR_LINE = re.compile(r"出错|失败|Traceback|Error", re.I)
SECRET = re.compile(r"(authtoken=|MoodleSession[=:]\s*|Bearer\s+|token[=:]\s*)[^\s&\"',]+", re.I)


def _check(name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def scrub(text: str) -> str:
    """日志摘要里万一带了凭据，遮掉再给人看。"""
    return SECRET.sub(r"\1…", text)


# ---------------------------------------------------------------- 各项检查

def check_install() -> dict:
    exe = shutil.which("monash")
    where = f"monash {__version__} · Python {platform.python_version()} · {platform.system()} {platform.machine()}"
    if not exe:
        return _check("安装", WARN, where + "；命令行里找不到 monash",
                      "关掉终端重新打开；还不行就重新运行 README 里的安装命令")
    return _check("安装", OK, where)


def check_data_dir() -> dict:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        probe = HOME / ".doctor-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return _check("数据目录", FAIL, f"{HOME} 写不进去：{e}", "检查磁盘空间和文件夹权限")
    free = shutil.disk_usage(HOME).free / 1e9
    status = WARN if free < 1 else OK
    return _check("数据目录", status, f"{HOME}（剩余 {free:.1f} GB）",
                  "磁盘快满了，课件同步可能失败" if status == WARN else "")


def check_browser() -> dict:
    from .browser_login import find_browser
    exe = find_browser()
    if not exe:
        return _check("登录用的浏览器", FAIL, "没找到 Chrome / Edge / Brave",
                      "装一个 Chrome 或 Edge；装在别处的话 monash config browser <路径>")
    return _check("登录用的浏览器", OK, exe)


def check_moodle() -> dict:
    from . import features as F
    fallback = "；截止日期暂时靠日历订阅链接" if F.load_calendar_url() else ""
    try:
        left = MoodleClient().time_remaining()
    except MoodleAuthError as e:
        return _check("Moodle 登录", FAIL, f"{e}{fallback}", "monash login")
    except (MoodleError, urllib.error.URLError, OSError) as e:
        return _check("Moodle 登录", WARN, f"连不上 Moodle：{e}", "检查网络，过一会儿再试")
    return _check("Moodle 登录", OK, f"已登录，会话还剩 {(left or 0) // 60} 分钟")


def check_ed() -> dict:
    if not jobs.have_ed_token():
        return _check("Ed 令牌", FAIL, "还没设置", "monash login ed")
    try:
        user = edlib.EdClient().me().get("user", {})
    except edlib.EdAuthError:
        return _check("Ed 令牌", FAIL, "令牌失效了（在 Ed 设置页被删了？）", "monash login ed，粘贴一个新令牌")
    except (urllib.error.URLError, OSError) as e:
        return _check("Ed 令牌", WARN, f"连不上 Ed：{e}", "检查网络，过一会儿再试")
    return _check("Ed 令牌", OK, f"有效（{user.get('name') or '已登录'}）")


def check_courses() -> dict:
    parts = []
    try:
        con = db_connect()
        moodle = [r["code"] or str(r["id"]) for r in con.execute("SELECT id, code FROM courses WHERE tracked=1")]
        con.close()
        parts.append("Moodle " + ("、".join(moodle) or "无"))
    except Exception:  # noqa: BLE001
        moodle = []
    ed = []
    try:
        conn = edlib.db_connect()
        ed = [(r["code"] or "").split()[0] for r in conn.execute("SELECT code FROM courses WHERE tracked=1")]
        conn.close()
        parts.append("Ed " + ("、".join(ed) or "无"))
    except Exception:  # noqa: BLE001
        pass
    if not moodle and not ed:
        return _check("跟踪的课", WARN, "还没有", "登录后运行 monash sync，或 monash courses 手动选")
    return _check("跟踪的课", OK, "；".join(parts))


def check_sync() -> dict:
    st = jobs.read_status()
    if not st.get("last_sync"):
        return _check("同步", WARN, "还没同步过", "monash sync")
    hours = jobs.hours_since_sync(st)
    when = datetime.fromisoformat(st["last_sync"]).astimezone().strftime("%m-%d %H:%M")
    errs = {k: v for k, v in (st.get("errors") or {}).items() if v}
    detail = f"上次 {when}（{hours:.0f} 小时前）" + "".join(f"；{k}：{scrub(str(v))[:120]}" for k, v in errs.items())
    if errs:
        return _check("同步", WARN, detail, "按上面的登录项修好后 monash sync")
    if hours > 48:
        return _check("同步", WARN, detail, "Claude 开着时会自动同步；也可以手动 monash sync")
    return _check("同步", OK, detail)


def check_claude_desktop() -> dict:
    path = claude_setup.desktop_config_path()
    if path is None:
        return _check("Claude Desktop", WARN, "没装，或者从没打开过", "只用 Claude Code / 其他客户端的话可以忽略")
    if not path.exists():
        return _check("Claude Desktop", FAIL, "还没接入 monash", "monash setup claude")
    try:
        entry = json.loads(path.read_text(encoding="utf-8") or "{}").get("mcpServers", {}).get(claude_setup.SERVER_NAME)
    except ValueError:
        return _check("Claude Desktop", FAIL, f"配置文件不是合法的 JSON：{path}", "monash setup claude（会先备份）")
    if not entry:
        return _check("Claude Desktop", FAIL, "配置里没有 monash", "完全退出 Claude 后运行 monash setup claude")
    cmd = entry.get("command", "")
    if not (shutil.which(cmd) or (cmd and _exists(cmd))):
        return _check("Claude Desktop", FAIL, f"配置里的程序找不到：{cmd}",
                      "重装过或挪过位置：完全退出 Claude 后运行 monash setup claude")
    return _check("Claude Desktop", OK, "已接入 monash")


def _exists(path: str) -> bool:
    from pathlib import Path
    return Path(path).exists()


def check_claude_code() -> dict:
    claude = shutil.which("claude")
    if not claude:
        return _check("Claude Code", OK, "没装（不影响 Claude Desktop）")
    try:
        r = subprocess.run([claude, "mcp", "get", claude_setup.SERVER_NAME], capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return _check("Claude Code", WARN, "查不了（claude 命令没响应）")
    if r.returncode != 0:
        return _check("Claude Code", WARN, "装了但没接入 monash", "monash setup claude")
    return _check("Claude Code", OK, "已接入 monash")


def check_mcp_log(lines: int = 400) -> dict:
    """Claude 里用的时候出的错只在 mcp.log 里，挑最近几条给人看。"""
    try:
        tail = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return _check("Claude 里的运行记录", OK, "还没有（Claude 还没启动过 monash）")
    started = [l for l in tail if "MCP 启动" in l]
    errors = [scrub(l) for l in tail if ERROR_LINE.search(l) and not l.startswith((" ", "\t"))]
    last = started[-1][:16] if started else "（最近没有）"
    if errors:
        return _check("Claude 里的运行记录", WARN, f"最近启动 {last}；最近的报错：" + " | ".join(e[:160] for e in errors[-3:]),
                      f"完整日志在 {LOG_FILE}")
    return _check("Claude 里的运行记录", OK, f"最近启动 {last}，没有报错")


def check_settings() -> dict:
    changed = {k: v for k, v in load_settings().items() if DEFAULTS.get(k) != v}
    files = f"课件在 {FILES_DIR}"
    return _check("设置", OK, files + ("；改过的设置：" + json.dumps(changed, ensure_ascii=False) if changed else ""))


CHECKS = [check_install, check_data_dir, check_browser, check_moodle, check_ed, check_courses, check_sync,
          check_claude_desktop, check_claude_code, check_mcp_log, check_settings]


def run(skip: tuple = ()) -> dict:
    results = []
    for fn in CHECKS:
        if fn.__name__ in skip:
            continue
        try:
            results.append(fn())
        except Exception as e:  # noqa: BLE001 —— 体检本身不能崩
            results.append(_check(fn.__name__.removeprefix("check_"), WARN, f"检查时出错：{type(e).__name__}: {e}"))
    return {"version": __version__, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ok": not any(r["status"] == FAIL for r in results), "checks": results}


def render(report: dict) -> str:
    out = []
    for r in report["checks"]:
        out.append(f"{MARK[r['status']]} {r['name']}：{r['detail']}")
        if r["fix"] and r["status"] != OK:
            out.append(f"    → {r['fix']}")
    bad = [r for r in report["checks"] if r["status"] == FAIL]
    out.append("")
    out.append("都没问题。" if not bad and all(r["status"] == OK for r in report["checks"])
               else f"{len(bad)} 项有问题（✗），先按 → 后面的做。" if bad else "没有大问题，! 的几项可以看一下。")
    out.append("需要别人帮忙时，把上面整段发过去就行（不包含密码、令牌之类的东西）。")
    return "\n".join(out)

