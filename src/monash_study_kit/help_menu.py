"""功能大全和预设提示：让朋友知道能问什么。

工具不是靠关键词触发的（Claude 按意思挑工具），但人不知道有什么功能就想不到去问。所以：
  * FEATURES：按类别列出能做的事，每条一句示例问法和对应的命令。`monash help` 打印它，
    MCP 的“功能大全”提示也用它，两边不会对不上。
  * PROMPTS：MCP 预设提示。它们只在用户从 Claude 的“+”菜单里选中时才发给 Claude，
    平时不占额度，这是它们比多加一个 help 工具划算的地方（工具清单每次对话都要整份发）。
"""
from __future__ import annotations

# (类别, [(能做什么, 示例问法, 命令行)])
FEATURES = [
    ("这周要做什么", [
        ("本周待办：截止、可能漏交、Ed 没做完的 lesson、最新公告", "这周我有什么要做的？", "monash todo"),
        ("截止日期（没登录时也能查）", "FIT2102 接下来两周有什么要交？", "monash due FIT2102"),
        ("作业提交状态、可能漏交", "我有没有漏交什么作业？", "monash moodle assignments --missing"),
    ]),
    ("课程资料", [
        ("全文搜课件、课程笔记网页、Ed 课件、录播字幕稿，带页码/时间戳", "哪一周讲了 monad？在哪份讲义第几页？",
         'monash grep "monad"'),
        ("读课件原文、帮你解释", "帮我看看 A2 的 spec 要求什么", "monash open"),
        ("某周的课件和外部链接", "FIT2109 第 5 周有哪些资料？", "monash moodle ls FIT2109 5"),
    ]),
    ("Ed 论坛", [
        ("上次看过之后的新帖和新回复", "Ed 上有什么新消息？", "monash ed new"),
        ("我发的/关注的帖子有没有人回", "我上次问的问题有人回了吗？", "monash ed following"),
        ("搜帖子、读全文", "有没有人问过 A1 能不能用 lodash？", "monash ed search FIT2102 lodash"),
        ("整节读 Ed lesson：正文、阅读网页、PDF 按页序拼好，附测验题", "带我过一遍 FIT2109 第 3 周的 pre-class",
         "monash ed read FIT2109 'W3 Pre-Class'"),
        ("Ed Lessons 进度和测验题（复习用）", "用第 3 周的测验题帮我复习", "monash ed quiz FIT2109 --module 'Week 3'"),
    ]),
    ("成绩和通知", [
        ("各课总分、每项得分和反馈", "我 FIT2109 现在成绩多少？", "monash moodle grades FIT2109"),
        ("Moodle 公告", "FIT2102 最近有什么公告？", "monash moodle news FIT2102"),
        ("Moodle 私信（只读）", "Moodle 上有人给我发消息吗？", "monash moodle messages"),
    ]),
    ("维护", [
        ("登录 / 重新登录 Moodle", "帮我重新登录 Moodle", "monash login"),
        ("马上同步一次", "同步一下最新的", "monash sync"),
        ("出问题时体检", "monash 用不了了", "monash doctor"),
    ]),
]


def render(cli: bool = True) -> str:
    """cli=True 给终端看（带命令）；False 给 Claude 看（只有功能和问法）。"""
    out = ["能做的事（直接用自己的话问就行，下面只是例子）：" if not cli else "monash 能做的事：", ""]
    for cat, items in FEATURES:
        out.append(f"■ {cat}")
        for what, ask, cmd in items:
            out.append(f"  · {what}")
            out.append(f"      问：“{ask}”" + (f"   命令：{cmd}" if cli else ""))
        out.append("")
    out.append("不能做的：交作业、做测验、在 Ed 上发帖，这些要自己去网页上操作。")
    if cli:
        out.append("每个命令都有 --help；Claude 里输入框的“+”菜单里有 monash 的预设提示，点一下就能用。")
    return "\n".join(out)


# ---------------------------------------------------------------- MCP 预设提示
# name → (标题, 说明, [(参数名, 说明, 必填)], 生成用户消息的函数)

def _todo(a):
    return ("这周我有什么要做的？按截止时间排，标出可能漏交的作业，"
            "再列 Ed 上还没做完的 lesson 和最近的公告。最后给我一个今天先做什么的建议。")


def _ed(a):
    course = f"（只看 {a['course']}）" if a.get("course") else ""
    return (f"Ed 上有什么新消息{course}？先说老师的公告和回答，再说我发的或关注的帖子有没有新回复，"
            "其余的一句话概括就行。")


def _topic(a):
    course = f"在 {a['course']} 里，" if a.get("course") else ""
    return (f"{course}“{a['topic']}”在哪份资料里讲过？列出课件名和页码或录播时间戳，"
            "然后读原文，用简单的话给我解释一遍。")


def _quiz(a):
    week = f"第 {a['week']} 周" if a.get("week") else "最近一周"
    return (f"用 {a['course']} Ed Lessons 里{week}的测验题帮我复习：一次出一道，等我回答后再判断对错并解释。"
            "Ed 不公开答案，请根据课件判断，拿不准的地方直接说。")


def _grades(a):
    return "我各门课现在的成绩和老师反馈怎么样？有没有需要特别注意的？"


def _help(a):
    return "我刚装了 monash。告诉我你能帮我做什么，按类别列出来，每类给我几句可以直接问的例子。\n\n" + render(cli=False)


PROMPTS = {
    "help": ("功能大全", "列出能做的所有事，每条附一句示例问法", [], _help),
    "weekly_todo": ("本周待办", "这周要做什么、有没有漏交", [], _todo),
    "ed_catchup": ("Ed 新消息", "公告、老师回答、我的帖子有没有人回", [("course", "只看某门课，如 FIT2102", False)], _ed),
    "find_topic": ("找知识点", "某个概念在哪份讲义/哪节录播，并解释",
                   [("topic", "要找的知识点，如 monad", True), ("course", "课号，可不填", False)], _topic),
    "quiz_review": ("测验题复习", "用 Ed Lessons 的测验题一题一题练",
                    [("course", "课号，如 FIT2109", True), ("week", "第几周，可不填", False)], _quiz),
    "grades": ("成绩和反馈", "各课成绩和老师反馈", [], _grades),
}


def prompt_list() -> list[dict]:
    return [{"name": name, "title": title, "description": desc,
             "arguments": [{"name": n, "description": d, "required": req} for n, d, req in args]}
            for name, (title, desc, args, _) in PROMPTS.items()]


def get_prompt(name: str, arguments: dict | None) -> dict:
    if name not in PROMPTS:
        raise KeyError(name)
    title, desc, args, build = PROMPTS[name]
    arguments = {k: v for k, v in (arguments or {}).items() if v not in (None, "")}
    missing = [n for n, _, req in args if req and n not in arguments]
    if missing:
        raise ValueError(f"缺少参数：{', '.join(missing)}")
    return {"description": desc,
            "messages": [{"role": "user", "content": {"type": "text", "text": build(arguments)}}]}
