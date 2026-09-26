"""终端输出：交互终端里打表格，管道或 --json 时打 JSON。"""
from __future__ import annotations

import json
import shutil
import sys
import unicodedata
from datetime import datetime, timezone


def want_json(args) -> bool:
    return getattr(args, "json", False) or not sys.stdout.isatty()


def emit_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def cut(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    if width(s) <= n:
        return s + " " * (n - width(s))
    out, w = "", 0
    for ch in s:
        cw = width(ch)
        if w + cw > n - 1:
            break
        out += ch
        w += cw
    return out + "…" + " " * (n - w - 1)


def table(rows: list[dict], cols: list[tuple[str, str, int]]) -> None:
    """cols: (key, 表头, 最大宽度)。最后一列吃掉剩余宽度。"""
    if not rows:
        print("（没有内容）")
        return
    term = shutil.get_terminal_size((120, 20)).columns
    widths = []
    for key, head, maxw in cols:
        w = max([width(head)] + [width(" ".join(str(r.get(key) or "").split())) for r in rows])
        widths.append(min(w, maxw))
    used = sum(widths[:-1]) + 2 * (len(cols) - 1)
    widths[-1] = max(10, min(widths[-1], term - used))
    print("  ".join(cut(h, w) for (_, h, _), w in zip(cols, widths)).rstrip())
    print("  ".join("─" * w for w in widths))
    for r in rows:
        print("  ".join(cut(r.get(k, ""), w) for (k, _, _), w in zip(cols, widths)).rstrip())


def local_time(iso: str | None, fmt: str = "%m-%d %a %H:%M") -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone().strftime(fmt)   # 按这台电脑的时区


def relative(iso: str | None) -> str:
    if not iso:
        return ""
    delta = datetime.fromisoformat(iso) - datetime.now(timezone.utc)
    secs = delta.total_seconds()
    if secs < 0:
        return "已过期"
    days = int(secs // 86400)
    return f"{days} 天后" if days else f"{int(secs // 3600)} 小时后"
