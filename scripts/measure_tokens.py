"""量一下 MCP 工具占多少 token：工具定义本身，以及几个常见问题的回答有多大。

    uv run python scripts/measure_tokens.py            # 只量工具定义，不需要登录
    uv run python scripts/measure_tokens.py --live     # 再用你自己同步好的数据跑几个常见问题
    uv run python scripts/measure_tokens.py --live --query "monad"

输出是一张 Markdown 表，只有数字，不包含课件内容、帖子或成绩，可以直接贴进 README。

token 数默认是估算：英文和符号按 4 个字符 1 个 token，中文等非 ASCII 字符按 1 个字符 1 个 token。
设了 ANTHROPIC_API_KEY 再加 --api，会改用 Anthropic 的 count_tokens 接口算准确值（这个接口不收费）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

COUNT_URL = "https://api.anthropic.com/v1/messages/count_tokens"


def estimate(text: str) -> int:
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return round(ascii_chars / 4 + (len(text) - ascii_chars))


def api_count(text: str, model: str) -> int:
    """Anthropic 的 count_tokens：一条只含这段文字的用户消息，减掉空消息的基数。"""
    def count(content: str) -> int:
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": content}]}).encode()
        req = urllib.request.Request(COUNT_URL, data=body, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
            "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["input_tokens"]
    return count(text) - count(".")


def tool_text(result: dict) -> str:
    return "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


def call(M, name: str, args: dict | None = None) -> str:
    r = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}})
    return tool_text(r["result"]) if "result" in r else json.dumps(r)


def definitions(M) -> list[tuple[str, str]]:
    tools = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    init = M.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})["result"]
    rows = [(f"tool `{t['name']}`", json.dumps(t, ensure_ascii=False)) for t in tools]
    rows.append(("server instructions", init.get("instructions", "")))
    return rows


def live(M, query: str) -> list[tuple[str, str, str | None]]:
    """(场景, 工具返回的文字, 不用工具时要贴给 Claude 的文字或 None)"""
    from monash_study_kit import content_index as CI
    from monash_study_kit.moodlelib import db_connect
    out = []
    out.append(("What's due this week (`get_study_todo`)", call(M, "get_study_todo"), None))
    out.append(("Upcoming deadlines (`list_moodle_due`)", call(M, "list_moodle_due"), None))
    out.append(("New on Ed (`list_ed_updates`)", call(M, "list_ed_updates"), None))

    con = db_connect()
    hits = CI.search(con, query, limit=10)
    con.close()
    found = call(M, "search_content", {"query": query})
    whole = []
    for path in dict.fromkeys(h["path"] for h in hits if h.get("path")):
        try:
            whole.append(call(M, "read_file", {"path": path, "max_chars": 100000}))
        except Exception:  # noqa: BLE001
            pass
    out.append((f"Find \"{query}\" in course files (`search_content`)", found,
                "\n".join(whole) if whole else None))
    if hits and hits[0].get("path"):
        page = call(M, "read_file", {"path": hits[0]["path"]})
        out.append(("Read the top hit, first page of output (`read_file`)", page, whole[0] if whole else None))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="用自己同步好的数据跑几个常见问题")
    ap.add_argument("--query", default="assignment", help="--live 时全文搜索用的词")
    ap.add_argument("--api", action="store_true", help="用 Anthropic count_tokens 接口算准确值")
    ap.add_argument("--model", default="claude-sonnet-5-5", help="--api 时用哪个模型的分词")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    if args.api and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--api 需要 ANTHROPIC_API_KEY")
    if not args.live:
        import tempfile
        os.environ.setdefault("MONASH_KIT_HOME", tempfile.mkdtemp(prefix="monash-measure-"))   # 空目录，不碰真实数据
    os.environ["MONASH_KIT_NO_UPDATE_CHECK"] = "1"
    from monash_study_kit import __version__, mcp_server as M

    count = (lambda t: api_count(t, args.model)) if args.api else estimate
    how = f"counted with the count_tokens API ({args.model})" if args.api else "estimated (4 ASCII chars or 1 CJK char per token)"

    rows = definitions(M)
    total = sum(count(t) for _, t in rows)
    print(f"monash-study-kit {__version__}, tokens {how}\n")
    print("| Sent once per conversation | Tokens |\n| --- | ---: |")
    for name, text in sorted(rows, key=lambda r: -count(r[1])):
        print(f"| {name} | {count(text):,} |")
    print(f"| **Total ({len(rows) - 1} tools + instructions)** | **{total:,}** |")

    if args.live:
        print("\n| Question | Tool answer | Without the tool | Saved |\n| --- | ---: | ---: | ---: |")
        for name, text, alt in live(M, args.query):
            a = count(text)
            if alt:
                b = count(alt)
                saved = f"{1 - a / b:.0%}" if b else "-"
                print(f"| {name} | {a:,} | {b:,} | {saved} |")
            else:
                print(f"| {name} | {a:,} | - | - |")
        print("\n\"Without the tool\" is the full text of every file the search hit, which is what you'd paste in otherwise.")


if __name__ == "__main__":
    main()
