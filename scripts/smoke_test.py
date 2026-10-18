"""装好的包能不能用：在一个全新的 venv 里装 wheel 之后跑这个。

只用标准库，不导入 monash_study_kit 本身，所以测的是用户真正会运行的 `monash` 命令：
  1. monash --version 的版本号和要发的版本一致
  2. monash help 能打印出来
  3. monash mcp 能完成 MCP 握手，tools/list 里工具都在、每个都有说明和参数格式

数据目录指到临时文件夹，不登录、不联网。

    python scripts/smoke_test.py path/to/venv/bin/monash 1.1.0
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

# 少了哪个就说明打包漏了模块或者工具注册坏了
EXPECTED_TOOLS = {
    "get_study_todo", "get_monash_status", "start_monash_login", "sync_monash_data",
    "list_courses", "list_moodle_due", "list_moodle_assignments", "get_moodle_grades",
    "search_moodle_forum", "get_moodle_messages", "search_content", "list_files", "read_file",
    "list_ed_updates", "search_ed_threads", "read_ed_thread", "get_ed_lessons",
}


def fail(msg: str) -> None:
    print(f"冒烟测试失败：{msg}", file=sys.stderr)
    sys.exit(1)


def run(exe: str, *args: str, env: dict, stdin: str | None = None) -> str:
    r = subprocess.run([exe, *args], input=stdin, capture_output=True, text=True,
                       encoding="utf-8", env=env, timeout=60)
    if r.returncode != 0:
        fail(f"monash {' '.join(args)} 退出码 {r.returncode}\n{r.stderr}")
    return r.stdout


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")   # Windows 的控制台默认 cp1252，打不出中文
    if len(sys.argv) != 3:
        fail("用法：smoke_test.py <monash 可执行文件> <版本号>")
    exe, version = sys.argv[1], sys.argv[2]
    home = tempfile.mkdtemp(prefix="monash-smoke-")
    env = {**os.environ, "MONASH_KIT_HOME": home, "MONASH_KIT_NO_UPDATE_CHECK": "1",
           "PYTHONIOENCODING": "utf-8"}
    env.pop("ED_TOKEN", None)

    out = run(exe, "--version", env=env).strip()
    if out != f"monash-study-kit {version}":
        fail(f"版本号不对：{out!r}，应该是 monash-study-kit {version}")
    print(f"ok  {out}")

    out = run(exe, "help", env=env)
    if "monash" not in out or len(out) < 200:
        fail(f"monash help 输出不对：{out[:200]!r}")
    print(f"ok  monash help（{len(out.splitlines())} 行）")

    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "prompts/list"},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in msgs)
    replies = {}
    for line in run(exe, "mcp", env=env, stdin=stdin).splitlines():
        if line.strip():
            msg = json.loads(line)
            replies[msg.get("id")] = msg
    if set(replies) != {1, 2, 3}:
        fail(f"MCP 回复的 id 是 {sorted(replies)}，应该是 [1, 2, 3]（通知不该有回复）")
    info = replies[1].get("result", {}).get("serverInfo", {})
    if info.get("version") != version:
        fail(f"serverInfo 版本是 {info.get('version')!r}")
    tools = replies[2].get("result", {}).get("tools", [])
    names = {t.get("name") for t in tools}
    if missing := EXPECTED_TOOLS - names:
        fail(f"tools/list 少了 {sorted(missing)}")
    for t in tools:
        if not t.get("description") or t.get("inputSchema", {}).get("type") != "object":
            fail(f"工具 {t.get('name')} 没有说明或参数格式")
    print(f"ok  MCP 握手，{len(tools)} 个工具，{len(replies[3]['result']['prompts'])} 个提示")


if __name__ == "__main__":
    main()
