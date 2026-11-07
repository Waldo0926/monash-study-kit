"""从课件里抽文字：PDF（按页）、docx/pptx（按段/页）。

PDF 默认用 pypdf（纯 Python，随包安装）；朋友的电脑上多半没有 pdftotext，Windows 更没有。
装了 poppler 的机器（比如服务器）可以 `monash config pdf_extractor pdftotext`，排版更准。
"""
from __future__ import annotations

import html
from collections import OrderedDict
import threading
import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

logging.getLogger("pypdf").setLevel(logging.ERROR)   # 课件 PDF 常有小毛病，pypdf 会刷一堆警告


_pdf_cache = OrderedDict()
_pdf_cache_lock = threading.Lock()
PDF_CACHE_CHARS = 8_000_000


def pdf_pages(path: Path, page_start: int | None = None, page_end: int | None = None) -> list[str]:
    """按文件版本和页范围缓存，限制条目和总字符数；失败结果不缓存。"""
    from .paths import load_settings
    stat = path.stat()
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, load_settings().get("pdf_extractor"), page_start, page_end)
    with _pdf_cache_lock:
        if key in _pdf_cache:
            _pdf_cache.move_to_end(key)
            return list(_pdf_cache[key])
    pages = _pdf_pages(path, page_start, page_end)
    if pages and any(pages) and sum(map(len, pages)) <= PDF_CACHE_CHARS:
        with _pdf_cache_lock:
            _pdf_cache[key] = tuple(pages)
            while len(_pdf_cache) > 16 or sum(len(p) for ps in _pdf_cache.values() for p in ps) > PDF_CACHE_CHARS:
                _pdf_cache.popitem(last=False)
    return pages


def _pdf_pages(path: Path, page_start: int | None = None, page_end: int | None = None) -> list[str]:
    """每页一段文字；坏文件、加密文件返回空列表。"""
    from .paths import load_settings
    if load_settings().get("pdf_extractor") == "pdftotext" and shutil.which("pdftotext"):
        pages = _pdftotext(path, page_start, page_end)
        if pages is not None:
            return pages
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return []
        out = []
        start = (page_start or 1) - 1
        end = min(page_end or len(reader.pages), len(reader.pages))
        for i in range(start, end):
            page = reader.pages[i]
            try:
                out.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001  一页坏了不影响别的页
                out.append("")
        return out
    except Exception:  # noqa: BLE001
        return []


def _pdftotext(path: Path, page_start: int | None = None, page_end: int | None = None) -> list[str] | None:
    try:
        args = ["pdftotext", "-layout"]
        if page_start is not None:
            args += ["-f", str(page_start)]
        if page_end is not None:
            args += ["-l", str(page_end)]
        r = subprocess.run([*args, str(path), "-"], capture_output=True, text=True,
                           timeout=120, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    pages = r.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()                     # 最后一页后面也有一个换页符
    return pages


def _xml_text(xml: str, para_end: str, sep: str) -> str:
    """去掉标签、还原 &amp; &lt; 这些转义。

    一个词常被拆成好几个 run（<a:t>Hel</a:t><a:t>lo</a:t>），所以标签之间不加空格，
    只在段落结尾加 sep，不然搜 "Hello" 会搜不到。"""
    xml = re.sub(r"<[aw]:(?:br|tab)\b[^>]*/>", " ", re.sub(para_end, sep, xml))   # 换行、制表符当空格
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


OFFICE_XML_MAX = 50 * 1024 * 1024   # 单个 slide/document.xml 解压后最多读这么多，防解压炸弹


def _read_member(z: zipfile.ZipFile, name: str) -> str:
    with z.open(name) as f:
        data = f.read(OFFICE_XML_MAX + 1)
    if len(data) > OFFICE_XML_MAX:
        raise ValueError(f"{name} 解压后太大，不读")
    return data.decode(errors="replace")


def office_parts(path: Path) -> list[tuple[str, str]]:
    """pptx → [("slide N", 文字)]；docx → [("", 全文，段落间空行)]。"""
    with zipfile.ZipFile(path) as z:
        if path.suffix.lower() == ".pptx":
            nums = sorted(int(m.group(1)) for n in z.namelist()
                          if (m := re.match(r"ppt/slides/slide(\d+)\.xml$", n)))
            return [(f"slide {i}", " ".join(_xml_text(_read_member(z, f"ppt/slides/slide{i}.xml"),
                                                      r"</a:p>", " ").split()))
                    for i in nums]
        xml = _read_member(z, "word/document.xml")
    return [("", _xml_text(xml, r"</w:p>", "\n\n"))]
