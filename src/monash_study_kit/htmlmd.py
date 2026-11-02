"""把 Moodle 页面里的一块 HTML 转成 Markdown（Page 正文、Book 章节、作业说明、评分细则）。

只处理课程页面里实际会出现的东西：标题、段落、列表、链接、图片、表格、代码、引用。
图片和链接可以交给调用方改写（比如图片下载到 assets/ 后换成相对路径）。
"""
from __future__ import annotations

import re
import urllib.parse

from . import htmldom

SKIP = {"script", "style", "noscript", "button", "form", "input", "select", "textarea", "nav", "svg"}
BLOCK = {"p", "div", "section", "article", "header", "footer", "figure", "figcaption", "main", "aside",
         "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "pre", "blockquote", "hr", "dl", "dt", "dd"}


def _clean(s: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", s)


class _Converter:
    def __init__(self, base_url: str, on_image, on_link):
        self.base = base_url
        self.on_image = on_image
        self.on_link = on_link

    def url(self, href: str | None) -> str:
        return urllib.parse.urljoin(self.base, href or "") if href else ""

    # ------------------------------------------------------------ 行内

    def inline(self, node) -> str:
        if isinstance(node, str):
            return _clean(node)
        tag = node.tag
        if tag in SKIP:
            return ""
        if tag == "br":
            return "  \n"
        if tag == "img":
            src = self.url(node.get("src"))
            if not src or src.startswith("data:"):
                return ""
            alt = (node.get("alt") or "").replace("]", "")
            new = self.on_image(src) if self.on_image else src
            if new == "":                    # 调用方说这是装饰图片（图标、视频缩略图），不要
                return alt
            return f"![{alt}]({new})"
        inner = "".join(self.inline(c) for c in node.children)
        if tag in ("strong", "b"):
            return f"**{inner.strip()}**" if inner.strip() else inner
        if tag in ("em", "i"):
            return f"*{inner.strip()}*" if inner.strip() else inner
        if tag == "code":
            return f"`{inner.strip()}`" if inner.strip() else ""
        if tag == "a":
            if (node.get("href") or "").startswith("#"):       # 页面内的跳转锚点，导出后没有意义
                return inner
            href = self.url(node.get("href"))
            text = " ".join(inner.split()) or href
            if not href or href.startswith(("javascript:", "#")):
                return inner
            return f"[{text}]({self.on_link(href, text) if self.on_link else href})"
        if tag in BLOCK:
            return " " + self.block(node).strip() + " "
        return inner

    # ------------------------------------------------------------ 块

    def block(self, node, depth: int = 0) -> str:
        if isinstance(node, str):
            return _clean(node)
        tag = node.tag
        if tag in SKIP:
            return ""
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self.inline_children(node).strip()
            return f"\n\n{'#' * int(tag[1])} {text}\n\n" if text else ""
        if tag == "p":
            text = self.inline_children(node).strip()
            return f"\n\n{text}\n\n" if text else ""
        if tag == "hr":
            return "\n\n---\n\n"
        if tag == "pre":
            return "\n\n```\n" + raw_text(node).strip("\n") + "\n```\n\n"
        if tag == "blockquote":
            inner = self.children(node, depth).strip()
            return "\n\n" + "\n".join("> " + l for l in inner.splitlines()) + "\n\n"
        if tag in ("ul", "ol"):
            return "\n\n" + self.list_(node, depth) + "\n\n"
        if tag == "table":
            return "\n\n" + table_markdown(node, self) + "\n\n"
        if tag == "img":
            return "\n\n" + self.inline(node) + "\n\n"
        if tag in BLOCK:
            return "\n" + self.children(node, depth) + "\n"
        return self.inline(node)

    def inline_children(self, node) -> str:
        return "".join(self.inline(c) for c in node.children)

    def children(self, node, depth: int = 0) -> str:
        out, run = [], []
        for c in node.children:
            if isinstance(c, htmldom.Node) and (c.tag in BLOCK or c.tag == "img" and c.parent is node and
                                                 node.tag not in ("p", "a", "span")):
                if run:
                    out.append("".join(self.inline(r) for r in run))
                    run = []
                out.append(self.block(c, depth))
            else:
                run.append(c)
        if run:
            out.append("".join(self.inline(r) for r in run))
        return "".join(out)

    def list_(self, node, depth: int) -> str:
        lines = []
        for i, li in enumerate(c for c in node.children if isinstance(c, htmldom.Node) and c.tag == "li"):
            mark = f"{i + 1}." if node.tag == "ol" else "-"
            parts, nested = [], []
            for c in li.children:
                if isinstance(c, htmldom.Node) and c.tag in ("ul", "ol"):
                    nested.append(self.list_(c, depth + 1))
                else:
                    parts.append(self.block(c, depth + 1) if isinstance(c, htmldom.Node) and c.tag in BLOCK
                                 else self.inline(c))
            text = " ".join(" ".join(parts).split())
            lines.append("  " * depth + f"{mark} {text}")
            lines += nested
        return "\n".join(lines)


def raw_text(node) -> str:
    """原样的文字（代码块要保留换行和缩进）。"""
    if isinstance(node, str):
        return node
    if node.tag == "br":
        return "\n"
    return "".join(raw_text(c) for c in node.children)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def table_markdown(node, conv: _Converter | None = None) -> str:
    """普通表格转成 Markdown 表格。第一行当表头；合并单元格按出现顺序摊平。"""
    conv = conv or _Converter("", None, None)
    rows = []
    for tr in node.find_all("tr"):
        # 嵌套表格里的行属于里面那张表，这里跳过
        if tr.parent is not None and _owner_table(tr) is not node:
            continue
        cells = [c for c in tr.children if isinstance(c, htmldom.Node) and c.tag in ("td", "th")]
        if cells:
            rows.append([_cell(conv.children(c)) for c in cells])
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _owner_table(node):
    p = node.parent
    while p is not None and p.tag != "table":
        p = p.parent
    return p


def to_markdown(fragment, base_url: str = "", on_image=None, on_link=None) -> str:
    """fragment 是 HTML 字符串或 htmldom.Node。on_image(url) / on_link(url, text) 返回改写后的地址。"""
    node = htmldom.parse(fragment) if isinstance(fragment, str) else fragment
    md = _Converter(base_url, on_image, on_link).children(node)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"
