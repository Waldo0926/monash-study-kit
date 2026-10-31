"""考勤的两件后台小事：记下登录能维持多久，按课表决定什么时候自动同步。

登录时长：每次登录、每次读成功、第一次发现过期都记一笔。过期时就知道这次登录在
“最后一次成功”和“第一次失败”之间失效；没过期就知道至少能撑多久。保留最近 10 次，
够看出规律，再决定续期要多频繁。

自动同步不按小时跑，按课表：工作日早上 8 点读一次（看这几天的班次），每节还没签到
的课开始 15 分钟后再读一次（看签上没有）。晚上、周末、没课的时候都不跑。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from datetime import time as clock

from .paths import DATA_DIR, write_secret

SESSION_FILE = DATA_DIR / "attendance" / "session.json"
MORNING = clock(8, 0)
AFTER_START = timedelta(minutes=15)
KEEP = 10


def _load() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    write_secret(SESSION_FILE, json.dumps(data))


def record_login() -> None:
    data = _load()
    if data.get("login_at"):
        past = {k: data.get(k) for k in ("login_at", "last_ok", "first_failure")}
        data["history"] = ([past] + data.get("history", []))[:KEEP]
    now = time.time()
    data.update(login_at=now, last_ok=now, first_failure=None)
    _save(data)


def record_ok() -> None:
    data = _load()
    data["last_ok"] = time.time()
    _save(data)


def record_failure() -> None:
    data = _load()
    if not data.get("first_failure"):
        data["first_failure"] = time.time()
        _save(data)


def _hours(seconds) -> float:
    return round(seconds / 3600, 1)


def _measure(entry: dict) -> dict | None:
    if not entry.get("login_at"):
        return None
    at_least = _hours((entry.get("last_ok") or entry["login_at"]) - entry["login_at"])
    if entry.get("first_failure"):
        return {"expired_after_hours": [at_least, _hours(entry["first_failure"] - entry["login_at"])]}
    return {"still_valid_after_hours": at_least}


def lifetime() -> dict:
    """这次登录的情况，加上以前几次登录各维持了多久。只有时长，没有别的信息。"""
    data = _load()
    current = _measure(data)
    if current is None:
        return {"current": "还没记录到登录"}
    past = [m for m in (_measure(e) for e in data.get("history", [])) if m]
    return {"current": current, "past_logins": past}


def last_auto_run() -> float | None:
    return _load().get("last_auto_run")


def mark_auto_run() -> None:
    data = _load()
    data["last_auto_run"] = time.time()
    _save(data)


def _start(record: dict, tz) -> datetime | None:
    try:
        return datetime.strptime(record["date"] + " " + record["time"].replace(" ", ""),
                                 "%Y-%m-%d %I:%M%p").replace(tzinfo=tz)
    except (KeyError, ValueError):
        return None


def due(records: list[dict], last_run: float | None, now: datetime) -> tuple[str, int] | None:
    """现在该不该自动读一次。该的话返回 (原因, 往前读几天)，否则 None。

    只看今天的时间点；错过的时间点（比如电脑睡着了）醒来后补一次，不会一个个补。
    """
    today = now.date()
    slots = []
    if today.weekday() < 5:
        slots.append((datetime.combine(today, MORNING, now.tzinfo), "morning", 3))
    for record in records:
        start = _start(record, now.tzinfo)
        if start and start.date() == today and not record.get("completed"):
            slots.append((start + AFTER_START, "after_class", 1))
    for slot, reason, days in sorted(slots, key=lambda s: s[0], reverse=True):
        if slot <= now and (last_run is None or slot.timestamp() > last_run):
            return reason, days
    return None
