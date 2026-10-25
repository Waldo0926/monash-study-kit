"""功能大全和预设提示：命令真的存在、提示能取、协议正确、不占工具清单。"""
import io
import shlex
from contextlib import redirect_stderr

import pytest

from monash_study_kit import cli, help_menu as H
from monash_study_kit import mcp_server as M


def call(method, params=None):
    return M.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


@pytest.mark.parametrize("cmd", [c for _, items in H.FEATURES for *_, c in items])
def test_every_listed_command_parses(cmd):
    """帮助里写的命令改名/删掉时，这里会失败，免得教朋友用不存在的命令。"""
    argv = shlex.split(cmd)
    assert argv[0] == "monash"
    with redirect_stderr(io.StringIO()):
        args = cli.build_parser().parse_args(argv[1:])
    assert getattr(args, "fn", None)


def test_help_render_for_cli_and_claude():
    cli_text, claude_text = H.render(cli=True), H.render(cli=False)
    assert "monash todo" in cli_text and "monash todo" not in claude_text
    assert "这周我有什么要做的？" in claude_text and "不能做的" in claude_text


def test_prompts_advertised_and_fetchable():
    init = call("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert "prompts" in init["capabilities"] and "help" in init["instructions"]
    prompts = {p["name"]: p for p in call("prompts/list")["result"]["prompts"]}
    assert {"help", "weekly_todo", "ed_catchup", "find_topic", "quiz_review", "assignment_rules",
            "grades"} == set(prompts)
    assert prompts["find_topic"]["arguments"][0] == {"name": "topic", "description": "要找的知识点，如 monad",
                                                     "required": True}
    msg = call("prompts/get", {"name": "find_topic", "arguments": {"topic": "monad", "course": "FIT2102"}})
    text = msg["result"]["messages"][0]["content"]["text"]
    assert msg["result"]["messages"][0]["role"] == "user" and "monad" in text and "FIT2102" in text
    help_text = call("prompts/get", {"name": "help"})["result"]["messages"][0]["content"]["text"]
    assert "Ed 论坛" in help_text


def test_assignment_prompt_puts_the_brief_first():
    """作业说明和评分细则为准，Ed 只认教职员工；不一致时列出来让用户去问，不替用户下结论。"""
    msg = call("prompts/get", {"name": "assignment_rules", "arguments": {"assignment": "A2", "course": "FIT2099"}})
    text = msg["result"]["messages"][0]["content"]["text"]
    assert "FIT2099" in text and "A2" in text
    assert "评分细则" in text and "助教" in text and "出处" in text and "问老师" in text
    init = call("initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert "作业说明和评分细则为准" in init["instructions"] and "同学的只当线索" in init["instructions"]


def test_prompt_errors():
    assert call("prompts/get", {"name": "nope"})["error"]["code"] == -32602
    err = call("prompts/get", {"name": "quiz_review", "arguments": {"week": "3"}})["error"]
    assert err["code"] == -32602 and "course" in err["message"]


def test_prompts_do_not_grow_the_tool_catalog():
    assert "help" not in {t["name"] for t in call("tools/list")["result"]["tools"]}
