"""把同步生成的 Markdown 转成可打印 HTML；不接受原始 HTML，不读取磁盘或网络。"""
from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import PurePosixPath

STYLE = """
@page { size: A4; margin: 15mm; }
body { font: 11pt/1.55 system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif;
       color: #222; overflow-wrap: anywhere; }
h1,h2,h3,h4,h5,h6 { line-height: 1.25; break-after: avoid; }
p { margin: .65em 0; } img { max-width: 100%; height: auto; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 9pt; }
th,td { border: 1px solid #aaa; padding: 5px; vertical-align: top; overflow-wrap: anywhere; }
thead { display: table-header-group; } tr { break-inside: auto; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 8px; background: #f4f4f4; }
code { font-family: ui-monospace, 'SFMono-Regular', Consolas, monospace; font-size: 9pt; }
blockquote { border-left: 3px solid #bbb; margin-left: 0; padding-left: 12px; color: #555; }
a { color: #185a91; text-decoration: underline; } li > p { margin: .2em 0; }
"""
CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
LIST = re.compile(r"^( *)([-+*]|\d+[.)])\s+(.*)$")
TOKEN = re.compile(r"(`+)(.+?)\1|(!?)\[([^\]\n]*)\]\((<[^>\n]*>|[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)\)|"
                   r"(\*\*|__)(.+?)\6|(\*|_)([^\n]+?)\8|\\([\\`*{}\[\]()#+.!_|>~-])", re.S)


def _href(url: str, pdf_links: bool) -> str | None:
    url = url.strip()
    # 控制字符可能让浏览器把看似无害的地址重新解释成 javascript:。
    if any(ord(c) < 32 for c in url) or url.startswith(("//", "\\")):
        return None
    if re.match(r"^[^/?#]*\s[^/?#]*:", url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"", "http", "https", "mailto"}:
        return None
    if pdf_links and not parts.scheme and parts.path.lower().endswith(".md"):
        url = urllib.parse.urlunsplit(parts._replace(path=parts.path[:-3] + ".pdf"))
    return html.escape(url, quote=True)


def inline(text: str, image=None, pdf_links: bool = False) -> str:
    out, start = [], 0
    for m in TOKEN.finditer(text):
        out.append(html.escape(text[start:m.start()]))
        if m[1]:
            out.append("<code>" + html.escape(m[2]) + "</code>")
        elif m[3] is not None:
            url = m[5].strip().removeprefix("<").removesuffix(">")
            if m[3] == "!":
                src = image(url) if image else None
                alt = html.escape(m[4], quote=True)
                out.append(f'<img src="{html.escape(src, quote=True)}" alt="{alt}">' if src else alt)
            else:
                href = _href(url, pdf_links)
                label = inline(m[4], image, pdf_links)
                out.append(f'<a href="{href}">{label}</a>' if href is not None else label)
        elif m[6] or m[8]:
            tag, body = ("strong", m[7]) if m[6] else ("em", m[9])
            out.append(f"<{tag}>{inline(body, image, pdf_links)}</{tag}>")
        else:
            out.append(html.escape(m[10]))
        start = m.end()
    out.append(html.escape(text[start:]))
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [c.strip().replace(r"\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _table_rule(line: str) -> bool:
    return bool(line.strip()) and all(re.fullmatch(r":?-{3,}:?", c) for c in _cells(line))


def blocks(lines: list[str], image=None, pdf_links: bool = False) -> str:
    out, i = [], 0
    fmt = lambda s: inline(s, image, pdf_links)  # noqa: E731
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker, body = fence[1], []
            i += 1
            while i < len(lines) and not re.fullmatch(r"\s*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*", lines[i]):
                body.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(body)) + "</code></pre>")
            i += 1
        elif re.match(r"^ {0,3}#{1,6}\s", line):
            h = re.match(r"^\s*(#{1,6})\s+(.*?)(?:\s+#+)?$", line)
            out.append(f"<h{len(h[1])}>{fmt(h[2])}</h{len(h[1])}>")
            i += 1
        elif re.fullmatch(r"\s*(?:\*\s*){3,}|\s*(?:-\s*){3,}|\s*(?:_\s*){3,}", line):
            out.append("<hr>")
            i += 1
        elif line.lstrip().startswith(">"):
            body = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                body.append(re.sub(r"^\s*> ?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + blocks(body, image, pdf_links) + "</blockquote>")
        elif LIST.match(line):
            first = LIST.match(line)
            indent, ordered = len(first[1]), first[2][0].isdigit()
            tag = "ol" if ordered else "ul"
            number = int(re.match(r"\d+", first[2])[0]) if ordered else 1
            start = f' start="{number}"' if ordered else ""
            out.append(f"<{tag}{start}>")
            while i < len(lines):
                m = LIST.match(lines[i])
                if not m or len(m[1]) != indent or m[2][0].isdigit() != ordered:
                    break
                body, content_indent = [m[3]], m.start(3)
                i += 1
                while i < len(lines):
                    s = lines[i]
                    if not s.strip():
                        if i + 1 < len(lines) and len(lines[i + 1]) - len(lines[i + 1].lstrip()) > indent:
                            body.append("")
                            i += 1
                            continue
                        break
                    if len(s) - len(s.lstrip()) <= indent:
                        break
                    body.append(s[min(content_indent, len(s) - len(s.lstrip())):])
                    i += 1
                out.append("<li>" + blocks(body, image, pdf_links) + "</li>")
            out.append(f"</{tag}>")
        elif i + 1 < len(lines) and "|" in line and _table_rule(lines[i + 1]):
            headers = _cells(line)
            out.append("<table><thead><tr>" + "".join(f"<th>{fmt(c)}</th>" for c in headers) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                cells = (_cells(lines[i]) + [""] * len(headers))[:len(headers)]
                out.append("<tr>" + "".join(f"<td>{fmt(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
        else:
            body = [line]
            i += 1
            while i < len(lines) and lines[i].strip():
                s = lines[i]
                if (LIST.match(s) or re.match(r"^\s*(?:#{1,6}\s|>|`{3,}|~{3,}|---+\s*$|\*\*\*+\s*$)", s)
                        or (i + 1 < len(lines) and "|" in s and _table_rule(lines[i + 1]))):
                    break
                body.append(s)
                i += 1
            out.append("<p>" + "<br>\n".join(fmt(s.rstrip()) for s in body) + "</p>")
    return "\n".join(out)


def document(markdown: str, filename: str = "page.md", *, image=None, pdf_links: bool = False) -> str:
    lines = markdown.expandtabs(4).splitlines()
    # 标题只从代码块外取，避免把示例里的 # 当作页面名。
    title, fence = PurePosixPath(filename).stem, None
    for line in lines:
        m = re.match(r"^\s*(`{3,}|~{3,})", line)
        if m:
            if fence is None:
                fence = m[1][0]
            elif m[1][0] == fence:
                fence = None
        elif fence is None and line.startswith("# "):
            title = line[2:].strip()
            break
    return (f'<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{html.escape(CSP, quote=True)}">'
            f'<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>'
            + blocks(lines, image, pdf_links) + "</body></html>")
