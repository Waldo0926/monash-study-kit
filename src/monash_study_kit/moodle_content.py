"""从 Moodle 页面里取出要存下来的正文：Page 正文、Book 整本（打印视图）、作业说明、评分细则。

都是纯解析，不联网；下载和落盘在 syncer 里。Moodle 4.x 的页面结构：
    Page        div.generalbox（在 #region-main 里）
    Book 打印   /mod/book/tool/print/index.php?id=<cmid>，每章一个 div.book_chapter（打开它不算“查看”）
    作业说明    div.activity-description / #intro
    Rubric      .gradingform_rubric 里的 tr.criterion：td.description + 每个 td.level（.definition、.score）
    评分指南    .gradingform_guide 里的 tr.criterion：.criterionshortname、.criteriondescription、最高分
"""
from __future__ import annotations

from . import htmldom, htmlmd


def _main(doc):
    return doc.find(id="region-main") or doc.find(role="main") or doc


def page_body(html: str):
    """Page 的正文节点；找不到返回 None。"""
    doc = htmldom.parse(html)
    main = _main(doc)
    return main.find(cls="generalbox") or main.find(cls="no-overflow")


def book_chapters(html: str) -> list[tuple[str, object]]:
    """Book 打印视图里的 [(章节标题, 正文节点)]。"""
    doc = htmldom.parse(html)
    out = []
    for ch in doc.find_all(cls="book_chapter"):
        h = ch.find("h2") or ch.find("h3")
        title = h.text() if h else ""
        if h is not None and h.parent is not None:
            h.parent.children.remove(h)          # 标题单独写成 ## ，正文里不再重复
        out.append((title, ch))
    return out


def assign_intro(html: str):
    """作业说明的节点（不含附件清单，附件另外下载）；没有说明返回 None。"""
    doc = htmldom.parse(html)
    intro = doc.find(cls="activity-description") or doc.find(id="intro")
    if intro is None:
        return None
    for att in intro.find_all(cls="introattachments") + intro.find_all(cls="activity-attachments"):
        if att.parent is not None:
            att.parent.children.remove(att)
    return intro


def _text(node) -> str:
    return " ".join(node.text().split()) if node is not None else ""


def rubric_markdown(html: str) -> str | None:
    """学生能看到的 Rubric，转成 Markdown 表格：每行一个评分项，每个等级一列（描述 + 分数）。"""
    doc = htmldom.parse(html)
    box = doc.find(cls="gradingform_rubric")
    if box is None:
        return None
    rows = []
    for tr in box.find_all("tr", cls="criterion"):
        desc = _text(tr.find(cls="description"))
        levels = []
        for lv in tr.find_all(cls="level"):
            definition = _text(lv.find(cls="definition"))
            score = _text(lv.find(cls="score")).replace("points", "").replace("分", "").strip()
            levels.append(f"{definition} ({score})" if score else definition)
        if desc or levels:
            rows.append([desc, *levels])
    if not rows:
        return None
    width = max(len(r) for r in rows)
    head = ["评分项"] + [f"等级 {i}" for i in range(1, width)]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(htmlmd._cell(c) for c in r + [""] * (width - len(r))) + " |" for r in rows]
    return "\n".join(out) + "\n"


def guide_markdown(html: str) -> str | None:
    """学生能看到的评分指南（marking guide）：评分项、说明、最高分。"""
    doc = htmldom.parse(html)
    box = doc.find(cls="gradingform_guide")
    if box is None:
        return None
    rows = []
    for tr in box.find_all("tr", cls="criterion"):
        name = _text(tr.find(cls="criterionshortname"))
        desc = _text(tr.find(cls="criteriondescription"))
        score = _text(tr.find(cls="score"))
        if name or desc:
            rows.append([name, desc, score])
    if not rows:
        return None
    out = ["| 评分项 | 说明 | 分数 |", "|---|---|---|"]
    out += ["| " + " | ".join(htmlmd._cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def grading_markdown(html: str) -> tuple[str | None, str | None]:
    """(Markdown, 'rubric' / 'guide')；页面上没给学生看评分细则就是 (None, None)。"""
    md = rubric_markdown(html)
    if md:
        return md, "rubric"
    md = guide_markdown(html)
    if md:
        return md, "guide"
    return None, None
