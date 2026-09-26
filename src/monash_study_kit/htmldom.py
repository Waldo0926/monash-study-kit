"""极简 HTML 树，给 Moodle 页面解析用（成绩表、论坛、作业、测验）。

标准库的 HTMLParser 只给事件流，这里把它搭成一棵树，再提供 find / find_all /
text 这几个最常用的操作。Moodle 的 HTML 大体规整，不需要 html5lib 那种容错。
"""
from __future__ import annotations

import html as _html
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}
# 这些标签遇到同级的新标签时自动闭合（HTML 允许省略结束标签）
AUTOCLOSE = {"p": {"p", "div", "ul", "ol", "table", "h1", "h2", "h3", "h4"},
             "li": {"li"}, "tr": {"tr"}, "td": {"td", "th", "tr"}, "th": {"td", "th", "tr"},
             "option": {"option"}}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict | None = None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent

    def get(self, key: str, default=None):
        return self.attrs.get(key, default)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self):
        for ch in self.children:
            if isinstance(ch, Node):
                yield ch
                yield from ch.iter()

    def _match(self, tag, cls, attrs) -> bool:
        if tag and self.tag not in (tag if isinstance(tag, (set, tuple, list)) else {tag}):
            return False
        if cls and not set(cls.split()) <= self.classes:
            return False
        for k, v in (attrs or {}).items():
            have = self.attrs.get(k)
            if v is True:
                if have is None:
                    return False
            elif callable(v):
                if have is None or not v(have):
                    return False
            elif have != v:
                return False
        return True

    def find_all(self, tag=None, cls: str | None = None, **attrs) -> list["Node"]:
        attrs = {k.rstrip("_").replace("__", "-"): v for k, v in attrs.items()}
        return [n for n in self.iter() if n._match(tag, cls, attrs)]

    def find(self, tag=None, cls: str | None = None, **attrs) -> "Node | None":
        attrs = {k.rstrip("_").replace("__", "-"): v for k, v in attrs.items()}
        return next((n for n in self.iter() if n._match(tag, cls, attrs)), None)

    def text(self, sep: str = " ") -> str:
        parts: list[str] = []

        def walk(n: Node):
            for ch in n.children:
                if isinstance(ch, str):
                    parts.append(ch)
                elif ch.tag not in ("script", "style"):
                    if ch.tag in ("br", "p", "div", "li", "tr") and parts:
                        parts.append(sep)
                    walk(ch)
        walk(self)
        return " ".join("".join(parts).split()) if sep == " " else \
            "\n".join(" ".join(line.split()) for line in "".join(parts).split(sep) if line.strip())

    def inner_html(self) -> str:
        out = []

        def walk(n: Node):
            for ch in n.children:
                if isinstance(ch, str):
                    out.append(_html.escape(ch, quote=False))
                else:
                    attrs = "".join(f' {k}="{_html.escape(v or "")}"' for k, v in ch.attrs.items())
                    out.append(f"<{ch.tag}{attrs}>")
                    if ch.tag not in VOID:
                        walk(ch)
                        out.append(f"</{ch.tag}>")
        walk(self)
        return "".join(out)

    def __repr__(self):
        return f"<{self.tag} {self.attrs.get('class', '')!r}>"


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        closes = AUTOCLOSE.get(self.cur.tag)
        if closes and tag in closes:
            self._close(self.cur.tag)
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        self._close(tag)

    def _close(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html_text: str) -> Node:
    b = _Builder()
    b.feed(html_text)
    b.close()
    return b.root


def form_fields(form: Node) -> list[tuple[str, str]]:
    """像浏览器一样收集一个表单要提交的字段（不含提交按钮）。"""
    out = []
    for n in form.iter():
        name = n.get("name")
        if not name or n.get("disabled") is not None:
            continue
        if n.tag == "input":
            t = (n.get("type") or "text").lower()
            if t in ("submit", "button", "image", "file", "reset"):
                continue
            if t in ("checkbox", "radio") and n.get("checked") is None:
                continue
            out.append((name, n.get("value") or ("on" if t in ("checkbox", "radio") else "")))
        elif n.tag == "textarea":
            out.append((name, "".join(c for c in n.children if isinstance(c, str))))
        elif n.tag == "select":
            opts = n.find_all("option")
            chosen = [o for o in opts if o.get("selected") is not None] or opts[:1]
            for o in chosen:
                out.append((name, o.get("value") if o.get("value") is not None else o.text()))
    return out
