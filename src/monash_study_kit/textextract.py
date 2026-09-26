"""从课件里抽文字：PDF（按页）、docx/pptx（按段/页）。

PDF 用 pypdf（纯 Python，随包安装）；朋友的电脑上多半没有 pdftotext，Windows 更没有。
"""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

logging.getLogger("pypdf").setLevel(logging.ERROR)   # 课件 PDF 常有小毛病，pypdf 会刷一堆警告


def pdf_pages(path: Path) -> list[str]:
    """每页一段文字；坏文件、加密文件返回空列表。"""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                return []
        out = []
        for page in reader.pages:
            try:
                out.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 —— 一页坏了不影响别的页
                out.append("")
        return out
    except Exception:  # noqa: BLE001
        return []


def office_parts(path: Path) -> list[tuple[str, str]]:
    """pptx → [("slide N", 文字)]；docx → [("", 全文，段落间空行)]。"""
    with zipfile.ZipFile(path) as z:
        if path.suffix.lower() == ".pptx":
            nums = sorted(int(m.group(1)) for n in z.namelist()
                          if (m := re.match(r"ppt/slides/slide(\d+)\.xml$", n)))
            return [(f"slide {i}", " ".join(re.sub(r"<[^>]+>", " ",
                                                    z.read(f"ppt/slides/slide{i}.xml").decode(errors="replace")).split()))
                    for i in nums]
        xml = z.read("word/document.xml").decode(errors="replace")
    return [("", re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n\n", xml)))]
