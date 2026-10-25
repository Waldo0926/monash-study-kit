"""skills/monash-study-kit/SKILL.md 是给 AI 看的说明书：里面写的命令和退出码要和 CLI 对得上。"""
import io
import re
import shlex
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from monash_study_kit import cli

SKILL = Path(__file__).resolve().parents[1] / "skills" / "monash-study-kit" / "SKILL.md"
TEXT = SKILL.read_text(encoding="utf-8")
# 表格里的是能直接跑的完整命令；正文里提到的（如 `monash grep`）可能没带参数，只查子命令存在
TABLE = sorted(set(re.findall(r"^\|.*`(monash [^`]+)` \|$", TEXT, re.M)))
MENTIONED = sorted(set(re.findall(r"`(monash [^`]+)`", TEXT)) - set(TABLE))


def test_frontmatter():
    head = TEXT.split("---")[1]
    assert re.search(r"^name: monash-study-kit$", head, re.M)
    desc = re.search(r"^description: (.+)$", head, re.M).group(1)
    assert len(desc) <= 1024 and "Read-only" in desc


@pytest.mark.parametrize("cmd", TABLE)
def test_every_command_parses(cmd):
    """命令改名或删掉时这里会失败，免得 AI 照着说明书跑不存在的命令。"""
    with redirect_stderr(io.StringIO()):
        args = cli.build_parser().parse_args(shlex.split(cmd)[1:])
    assert getattr(args, "fn", None)


@pytest.mark.parametrize("cmd", MENTIONED)
def test_every_mentioned_command_exists(cmd):
    with redirect_stdout(io.StringIO()), pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(shlex.split(cmd)[1:] + ["--help"])
    assert e.value.code == 0


def test_exit_codes_match_cli():
    rows = dict(re.findall(r"^\| (\d+) \| (.+) \|$", TEXT, re.M))
    assert sorted(map(int, rows)) == sorted(cli.EXIT_CODES)


def test_assignment_rules_and_limits():
    assert "brief and the marking rubric come first" in TEXT
    assert "Other students' replies are leads" in TEXT
    assert "Never ask for, accept or repeat an Ed token" in TEXT
