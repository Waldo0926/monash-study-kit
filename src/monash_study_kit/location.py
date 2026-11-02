"""数据放在哪：第一次 setup 时选位置，以后用 monash move 整体搬家。

很多同学的 C 盘很满，课件、录像、数据库动辄几个 GB，不该默认塞在 C 盘。选了别的位置后，
系统默认目录（Windows 是 %LOCALAPPDATA%\\monash-study-kit）里只留一个 location.json，
paths.py 启动时读它（见 paths.resolve_home）。

搬家只在本机挪文件，不联网。搬之前要先关掉 Claude 等 AI 软件（它们开着 MCP，会占着数据库）。
"""
from __future__ import annotations

import json
import os
import shutil
import string
import sys
from pathlib import Path

from . import paths

FOLDER_NAME = "MonashStudyKit"
LOW_SPACE = 5 * 1024 ** 3        # 剩余不到 5 GB 就建议换个盘


def free_bytes(path: Path) -> int | None:
    """path 所在磁盘的剩余空间；path 还不存在就往上找存在的那一级。"""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def candidates() -> list[dict]:
    """可选的位置：系统默认目录，Windows 上再加每个别的盘的 X:\\MonashStudyKit。剩余空间多的排前面（默认目录永远第一个）。"""
    out = [{"path": paths.DEFAULT_HOME, "free": free_bytes(paths.DEFAULT_HOME), "default": True}]
    if os.name == "nt":
        system = (os.environ.get("SystemDrive") or "C:").upper().rstrip("\\")
        drives = []
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:\\")
            if f"{letter}:" == system or not root.exists():
                continue
            free = free_bytes(root)
            if free:
                drives.append({"path": root / FOLDER_NAME, "free": free, "default": False})
        out += sorted(drives, key=lambda d: -d["free"])
    return out


def recommended(cands: list[dict]) -> int:
    """推荐哪一个（下标）：默认目录空间够就默认目录，否则剩余空间最多的别的盘。"""
    if (cands[0]["free"] or 0) >= LOW_SPACE or len(cands) == 1:
        return 0
    return max(range(1, len(cands)), key=lambda i: cands[i]["free"] or 0)


def gb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1024 ** 3:.1f} GB"


def write_pointer(home: Path, location_file: Path | None = None, default: Path | None = None) -> None:
    """记下数据的实际位置；放回默认位置时删掉 location.json。"""
    location_file = location_file or paths.LOCATION_FILE
    default = default or paths.DEFAULT_HOME
    if Path(home).resolve() == Path(default).resolve():
        location_file.unlink(missing_ok=True)
        return
    location_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = location_file.with_name(location_file.name + ".tmp")
    tmp.write_text(json.dumps({"home": str(Path(home).resolve())}, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, location_file)


class MoveError(RuntimeError):
    pass


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def move(src: Path, target: Path, *, location_file: Path | None = None, default: Path | None = None,
         log=print) -> dict:
    """把数据目录 src 整个挪到 target，再把 location.json 指过去。

    target 必须不存在或是空文件夹，也不能在 src 里面。默认目录里的 location.json 自己不挪。
    跨盘时 shutil.move 是先复制再删，中途失败的话已经挪过去的留在 target，src 里剩下的不动，
    重新运行同一条命令会接着挪。"""
    location_file = location_file or paths.LOCATION_FILE
    default = default or paths.DEFAULT_HOME
    src, target = Path(src), Path(target).expanduser()
    if not target.is_absolute():
        raise MoveError("新位置要写完整路径，比如 D:\\MonashStudyKit")
    if src.exists() and target.resolve() == src.resolve():
        raise MoveError(f"数据已经在 {src} 了")
    if _inside(target, src):
        raise MoveError("新位置不能在现在的数据目录里面")
    if target.exists() and not target.is_dir():
        raise MoveError(f"{target} 是个文件，换一个文件夹")
    skip = {location_file.name} if src.resolve() == Path(default).resolve() else set()
    entries = [p for p in (src.iterdir() if src.exists() else []) if p.name not in skip]
    clash = [p.name for p in entries if (target / p.name).exists()]
    if clash:
        raise MoveError(f"{target} 里已经有 {', '.join(sorted(clash)[:5])}，换一个空文件夹")
    need = sum(f.stat().st_size for p in entries for f in ([p] if p.is_file() else p.rglob("*")) if f.is_file())
    free = free_bytes(target)
    if free is not None and free < need:
        raise MoveError(f"{target} 所在的盘只剩 {gb(free)}，要 {gb(need)}")
    paths.ensure_private_dir(target)
    moved = 0
    for p in entries:
        log(f"  → {p.name}")
        shutil.move(str(p), str(target / p.name))
        moved += 1
    _fix_files_dir(target / "settings.json", src, target)
    write_pointer(target, location_file, default)
    # 搬走后旧目录如果空了（不是默认目录）就删掉
    if src.exists() and src.resolve() != Path(default).resolve() and not any(src.iterdir()):
        src.rmdir()
    return {"from": str(src), "to": str(target), "entries": moved, "bytes": need}


def _fix_files_dir(settings_file: Path, src: Path, target: Path) -> None:
    """设置里自定义的课件目录如果在旧数据目录里面，跟着改到新位置。"""
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    files_dir = data.get("files_dir")
    if not files_dir:
        return
    try:
        rel = Path(files_dir).expanduser().resolve().relative_to(src.resolve())
    except ValueError:
        return
    data["files_dir"] = str(target.resolve() / rel)
    settings_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ask(input_fn=input, print_fn=print) -> Path | None:
    """setup 第一次运行时问数据放哪。返回选的位置；选默认位置返回 None。"""
    cands = candidates()
    rec = recommended(cands)
    print_fn("数据（课件、数据库、登录状态）放在哪？课件多的话会有几个 GB。")
    for i, c in enumerate(cands, 1):
        tag = "（系统默认）" if c["default"] else ""
        tag += "  ← 推荐" if i - 1 == rec else ""
        print_fn(f"  {i}. {c['path']}  剩余 {gb(c['free'])}{tag}")
    print_fn("  也可以直接输入一个文件夹的完整路径。以后要换：monash move <新位置>")
    while True:
        ans = input_fn(f"选哪个？[{rec + 1}] ").strip().strip('"')
        if not ans:
            choice = cands[rec]["path"]
        elif ans.isdigit() and 1 <= int(ans) <= len(cands):
            choice = cands[int(ans) - 1]["path"]
        else:
            choice = Path(ans).expanduser()
            if not choice.is_absolute():
                print_fn("要写完整路径，比如 D:\\MonashStudyKit")
                continue
        return None if Path(choice).resolve() == paths.DEFAULT_HOME.resolve() else Path(choice)


def first_run() -> bool:
    """还没有任何数据、也没选过位置、也没用 MONASH_KIT_HOME 指定：setup 该问一下放哪。"""
    if os.environ.get("MONASH_KIT_HOME") or paths.LOCATION_FILE.exists():
        return False
    home = paths.DEFAULT_HOME
    return not home.exists() or not any(p for p in home.iterdir() if p.name != paths.LOCATION_FILE.name)


def restart_setup() -> None:
    """选了新位置后重新启动 setup：各模块在导入时就定下了目录，换位置要换个新进程。
    用子进程而不是 exec：Windows 上 exec 会把控制台的输入弄乱。"""
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "monash_study_kit", "setup"]))
