"""先选择账号目录，再导入会固定路径的 CLI 和服务模块。"""
from __future__ import annotations

import os
import sys

from .home import resolve_home, validate_profile


PROFILE_OVERRIDES = ("MONASH_KIT_DATA", "MONASH_KIT_SECRETS", "MONASH_KIT_FILES", "MONASH_KIT_ED_DATA",
                     "MONASH_KIT_ED_TOKEN", "ED_TOKEN")


def configure(argv: list[str]) -> list[str]:
    """从任意命令层级取出全局 --profile，参数分隔符 -- 之后按原样保留。"""
    remaining, names = [], []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            remaining.extend(argv[i:])
            break
        if arg == "--profile":
            i += 1
            if i == len(argv) or argv[i].startswith("--"):
                raise ValueError("--profile 后面要填账号名称")
            names.append(argv[i])
        elif arg.startswith("--profile="):
            names.append(arg.split("=", 1)[1])
        else:
            remaining.append(arg)
        i += 1
    if len(names) > 1:
        raise ValueError("一次只能选择一个 --profile")
    name = names[0] if names else os.environ.get("MONASH_KIT_PROFILE")
    if name not in (None, "default"):
        validate_profile(name)
    environ = dict(os.environ)
    # 子进程继承当前 profile 时，用记住的默认根目录，避免再套一层 profiles。
    if environ.get("_MONASH_KIT_PROFILE_HOME") == environ.get("MONASH_KIT_HOME") and environ.get("_MONASH_KIT_BASE_HOME"):
        environ["MONASH_KIT_HOME"] = environ["_MONASH_KIT_BASE_HOME"]
    base = resolve_home(environ)
    loaded = sys.modules.get(__package__ + ".paths")
    selected = None if name in (None, "default") else name
    expected = base / "profiles" / name if selected else base
    if loaded is not None and (loaded.HOME != expected or loaded.PROFILE != selected):
        raise ValueError("账号路径已初始化，请用新的 monash 进程选择 --profile，不能在同一进程内切换账号")
    if name in (None, "default"):
        if os.environ.get("_MONASH_KIT_PROFILE_HOME") == os.environ.get("MONASH_KIT_HOME") and os.environ.get("_MONASH_KIT_BASE_HOME"):
            os.environ["MONASH_KIT_HOME"] = str(base)
        os.environ.pop("MONASH_KIT_PROFILE", None)
    else:
        target = base / "profiles" / name
        if (base / "profiles").is_symlink() or target.is_symlink():
            raise ValueError("profile 目录不能是符号链接")
        os.environ.update(MONASH_KIT_HOME=str(target), MONASH_KIT_PROFILE=name,
                          _MONASH_KIT_BASE_HOME=str(base), _MONASH_KIT_PROFILE_HOME=str(target))
        for key in PROFILE_OVERRIDES:
            os.environ.pop(key, None)
        from .paths import ensure_private_dir
        ensure_private_dir(target.parent)
        ensure_private_dir(target)
    return remaining


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = configure(list(sys.argv[1:] if argv is None else argv))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    except OSError:
        print("profile 目录无法创建，请检查默认数据目录及权限", file=sys.stderr)
        return 1
    from .cli import main as cli_main
    return cli_main(arguments)
