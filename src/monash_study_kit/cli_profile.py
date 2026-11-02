"""仅在默认账号下管理命名 profile；删除前先吊销全部 Google 授权。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import google_export, paths
from .home import validate_profile

ED_HINT = "Ed 的 API 令牌没有吊销接口，请到 https://edstem.org/au/settings/api-tokens 手动删除"
SESSIONS = ("secrets", "browser-profile", "attendance-browser")
TOKENS = ("google-drive-token.json", "gmail-token.json")


def profile_path(name: str) -> Path:
    validate_profile(name)
    root = paths.HOME / "profiles"
    target = root / name
    if root.is_symlink() or target.is_symlink():
        raise ValueError("profile 目录不能是符号链接")
    return target


def _size(directory: Path, exclude_profiles=False) -> int:
    total = 0
    for base, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()
                   and not (exclude_profiles and Path(base) == directory and d == "profiles")]
        for name in files:
            file = Path(base) / name
            if not file.is_symlink():
                total += file.stat().st_size
    return total


def list_profiles() -> list[dict]:
    """只查凭据文件是否存在，不读其中内容。"""
    rows = []
    roots = [("default", paths.HOME)]
    root = paths.HOME / "profiles"
    if root.is_dir() and not root.is_symlink():
        for directory in sorted(root.iterdir()):
            try:
                validate_profile(directory.name)
            except ValueError:
                continue
            if directory.is_dir() and not directory.is_symlink():
                roots.append((directory.name, directory))
    for name, directory in roots:
        secrets = paths.SECRETS_DIR if name == "default" else directory / "secrets"
        safe = not secrets.is_symlink()
        exists = lambda filename: safe and (secrets / filename).is_file() and not (secrets / filename).is_symlink()
        rows.append({"name": name, "path": str(directory), "moodle": exists("moodle-cookie"),
                     "ed": exists("ed-token"), "google": any(exists(t) for t in TOKENS),
                     "bytes": _size(directory, exclude_profiles=name == "default")})
    return rows


def remove_profile(name: str, *, all_data: bool = False, yes: bool = False, ask=None, log=print) -> bool:
    """任何吊销失败都不删本机内容；不调用 Moodle 注销或任何课程接口。"""
    if not paths.is_default_profile():
        raise ValueError("请在默认账号下运行 monash profile，不能在命名 profile 内管理其他账号")
    target = profile_path(name)
    if not target.is_dir():
        raise ValueError(f"没有这个 profile：{name}")
    for part in SESSIONS:
        if (target / part).is_symlink():
            raise ValueError("登录或会话目录不能是符号链接，请先手动检查")
    for token in TOKENS:
        if (target / "secrets" / token).is_symlink():
            raise ValueError("凭据文件不能是符号链接，请先手动检查")
    deleting = [target] if all_data else [target / part for part in SESSIONS]
    if not yes:
        answer = (ask or input)("将删除以下本机目录，并先吊销 Google 授权：\n" + "\n".join(str(d) for d in deleting) + "\n确认删除？[y/N] ")
        if answer.strip().lower() not in {"y", "yes", "是"}:
            log("已取消，未删除任何文件")
            return False
    for token in TOKENS:
        google_export.revoke_token(target / "secrets" / token)
    if all_data:
        shutil.rmtree(target)
        log(f"已删除 profile：{target}")
    else:
        for part in SESSIONS:
            directory = target / part
            if directory.is_dir():
                shutil.rmtree(directory)
        log(f"已清除登录和会话，课程资料与数据库仍保留在：{target}")
    log(ED_HINT)
    return True


def run(args):
    try:
        if not paths.is_default_profile():
            raise ValueError("请在默认账号下运行 monash profile，不能在命名 profile 内管理其他账号")
        if args.profile_cmd == "list":
            for row in list_profiles():
                flag = lambda key: "有" if row[key] else "无"
                print(f"{row['name']} · Moodle {flag('moodle')} · Ed {flag('ed')} · Google {flag('google')} · {row['bytes']} 字节 · {row['path']}")
        else:
            remove_profile(args.name, all_data=args.all, yes=args.yes)
        return 0
    except (ValueError, OSError, google_export.GoogleError) as e:
        print(str(e) if not isinstance(e, OSError) else "本机目录清理失败，请检查权限后重试", file=sys.stderr)
        return 1


def add_commands(sub):
    p = sub.add_parser("list", help="列账号、凭据是否存在和占用空间")
    p.set_defaults(fn=run)
    p = sub.add_parser("remove", help="吊销 Google 授权并清除本机登录会话")
    p.set_defaults(fn=run)
    p.add_argument("name", help="命名 profile，不能是 default")
    p.add_argument("--all", action="store_true", help="同时删除课程资料和数据库")
    p.add_argument("--yes", action="store_true", help="直接确认所列目录的清理")
