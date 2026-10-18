"""发版前看一眼打出来的包：该有的都在，不该有的没混进去。

    uv build && python scripts/check_dist.py dist 1.1.0

查的是：
  * 正好一个 wheel 和一个 sdist，文件名里的版本号对
  * src/monash_study_kit 下每个 .py 都进了 wheel（漏一个，装好就 ImportError）
  * 没有 __pycache__、测试、数据库、cookie、令牌、.env 之类的东西
  * wheel 的 METADATA 里版本号、入口命令和 README 都在
"""
from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "monash_study_kit"
# 文件名像这些的一律不该打进包里
BAD = re.compile(r"(__pycache__|\.pyc$|(^|/)tests?/|\.db$|\.sqlite|cookie|token$|\.env$|\.pem$|\.key$)", re.I)


def fail(msg: str) -> None:
    print(f"打包检查失败：{msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")   # Windows 的控制台默认 cp1252，打不出中文
    if len(sys.argv) != 3:
        fail("用法：check_dist.py <dist 目录> <版本号>")
    dist, version = Path(sys.argv[1]), sys.argv[2]
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail(f"dist 里应该正好一个 wheel 和一个 sdist，现在是 {[p.name for p in dist.iterdir()]}")
    wheel, sdist = wheels[0], sdists[0]
    for p in (wheel, sdist):
        if f"-{version}" not in p.name:
            fail(f"{p.name} 的版本号不是 {version}")

    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        meta_name = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
        entry_name = next((n for n in names if n.endswith(".dist-info/entry_points.txt")), None)
        if not meta_name or not entry_name:
            fail("wheel 里没有 METADATA 或 entry_points.txt")
        meta = z.read(meta_name).decode("utf-8")
        entry = z.read(entry_name).decode("utf-8")

    want = {f"monash_study_kit/{p.relative_to(PKG).as_posix()}" for p in PKG.rglob("*.py")
            if "__pycache__" not in p.parts}
    if missing := want - set(names):
        fail(f"wheel 里少了 {sorted(missing)}")
    with tarfile.open(sdist) as t:
        sdist_names = [m.name.split("/", 1)[1] for m in t.getmembers() if "/" in m.name]
    for n in names + sdist_names:
        # sdist 带上测试是正常的（别人可以从源码包跑测试），wheel 不该带
        if BAD.search(n) and not (n in sdist_names and n.startswith("tests/") and n.endswith(".py")):
            fail(f"包里不该有 {n}")
    if f"\nVersion: {version}\n" not in meta:
        fail("METADATA 里的版本号不对")
    if "monash = monash_study_kit.cli:main" not in entry:
        fail("没有 monash 命令的入口")
    if "Description-Content-Type: text/markdown" not in meta or "## Installation" not in meta:
        fail("PyPI 页面会没有 README")
    print(f"ok  {wheel.name}（{len(names)} 个文件）")
    print(f"ok  {sdist.name}（{len(sdist_names)} 个文件）")


if __name__ == "__main__":
    main()
