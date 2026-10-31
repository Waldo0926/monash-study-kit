"""功能大全和预设提示：让朋友知道能问什么。

工具不是靠关键词触发的（Claude 按意思挑工具），但人不知道有什么功能就想不到去问。所以：
  * FEATURES：按类别列出能做的事，每条一句示例问法和对应的命令。`monash help` 打印它，
    MCP 的“功能大全”提示也用它，两边不会对不上。Gmail 和考勤在 OPTIONAL 里，开了才列。
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
        ("按章节或活动名定位 Moodle 项目", "找到 FIT2102 第 7 周的 workshop", "monash moodle find 'week 7' FIT2102"),
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
        ("在本机网页浏览已同步课件", "打开本地课件浏览页", "monash library --open"),
        ("登录 / 重新登录 Moodle", "帮我重新登录 Moodle", "monash login"),
        ("马上同步一次", "同步一下最新的", "monash sync"),
        ("出问题时体检", "monash 用不了了", "monash doctor"),
    ]),
]


# 可选功能：设置里打开了才列出来，没装没授权的人看到也用不了。键是对应的设置项。
# 写操作那几条在 AI 里先给预览，用户确认后才执行；命令行留空的只能在 AI 里用。
OPTIONAL = {
    "gmail_enabled": ("学校邮箱（Gmail）", [
        ("邮件待办：最近几天收件箱里没读的学校邮件，去掉推广和社交类", "我邮箱里有什么要处理的？", "monash gmail todo"),
        ("离线全文搜索已同步的邮件，带日期、未读、星标和附件数", "找一下交换申请的邮件", "monash gmail search 交换"),
        ("读整个邮件对话和附件列表", "把那封交换申请的邮件全文给我看", "monash gmail thread THREAD_ID"),
        ("读附件：PDF、Word、PPT、图片里的文字", "那封邮件附件里的表格要填什么？",
         "monash gmail attachment MESSAGE_ID PART_ID"),
    ]),
    "gmail_manage_enabled": ("邮箱操作（先预览，确认后才执行）", [
        ("起草回复、发送草稿", "帮我给老师起草一封请假邮件", "monash gmail draft --file reply.eml"),
        ("改标签：归档、标已读、加星标", "把这封邮件归档并标成已读", "monash gmail labels MESSAGE_ID --remove INBOX"),
    ]),
    "attendance_enabled": ("考勤", [
        ("最近的班次：哪些已签到、哪些还没签、哪些还没到时间", "这周还有哪节课没签到？", "monash attendance overview"),
        ("从 Moodle、Ed 和学校邮件里找签到码，标出处让你核对", "今天 FIT2102 workshop 的签到码是多少？",
         "monash attendance codes"),
        ("重新读一遍考勤门户（要开浏览器，一两分钟）", "刷新一下我的考勤", "monash attendance sync"),
    ]),
    "attendance_submit_enabled": ("签到提交（先预览，确认后才执行）", [
        ("你确认自己到课了，再用签到码帮你提交", "我刚上完 FIT2102 的 workshop，签到码是 ABCDE", ""),
    ]),
}
# 写操作要先开对应的只读功能
REQUIRES = {"gmail_manage_enabled": "gmail_enabled", "attendance_submit_enabled": "attendance_enabled"}


def features(settings: dict | None = None) -> list:
    """FEATURES 加上已开启的可选功能（放在“维护”前面）。"""
    if settings is None:
        from .paths import load_settings
        settings = load_settings()
    extra = [OPTIONAL[key] for key in OPTIONAL
             if settings.get(key) and settings.get(REQUIRES.get(key, key))]
    return FEATURES[:-1] + extra + FEATURES[-1:]


def render(cli: bool = True, settings: dict | None = None) -> str:
    """cli=True 给终端看（带命令）；False 给 Claude 看（只有功能和问法）。"""
    out = ["能做的事（直接用自己的话问就行，下面只是例子）：" if not cli else "monash 能做的事：", ""]
    for cat, items in features(settings):
        out.append(f"■ {cat}")
        for what, ask, cmd in items:
            out.append(f"  · {what}")
            out.append(f"      问：“{ask}”" + (f"   命令：{cmd}" if cli and cmd else ""))
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


def _assignment(a):
    course = f"{a['course']} 的" if a.get("course") else ""
    return (f"帮我弄清楚{course}“{a['assignment']}”的要求。先找到作业说明和评分细则读原文，"
            "再在 Ed 上搜这个作业的帖子，只采用教师和助教的回复、被 endorse 的回答，同学的回复只当线索。"
            "按要交什么、怎么评分、截止和格式、Ed 上的补充说明列出来，每条写出处"
            "（文件名和页码，或帖子编号、谁说的、日期）。"
            "作业说明和 Ed 不一致的地方单独列出，标明哪个更新，不要替我下结论，提醒我去问老师确认。")


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
    "assignment_rules": ("作业要求", "读作业说明、评分细则和老师在 Ed 上的答疑，每条标出处",
                         [("assignment", "作业名，如 A2", True), ("course", "课号，可不填", False)], _assignment),
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
