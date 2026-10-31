"""Bounded evidence collection from existing private mirrors; never invent codes."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from .paths import DATA_DIR, ED_DB, ensure_private_dir


def extract_file(filename, original_name):
    from . import textextract
    suffix = Path(original_name).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        prefix = os.environ.get("MONASH_KIT_TESSERACT_PREFIX")
        executable = str(Path(prefix)/"usr/bin/tesseract") if prefix else shutil.which("tesseract")
        if not executable or not Path(executable).is_file():
            raise RuntimeError("Image OCR needs the tesseract system package")
        environment = dict(os.environ)
        if prefix:
            environment["LD_LIBRARY_PATH"] = str(Path(prefix)/"usr/lib/x86_64-linux-gnu")
            environment["TESSDATA_PREFIX"] = str(Path(prefix)/"usr/share/tesseract-ocr/5/tessdata")
        from PIL import Image
        directory = DATA_DIR / "attendance" / "evidence"
        ensure_private_dir(directory)
        normalized = directory / (hashlib.sha256(Path(filename).read_bytes()).hexdigest() + ".png")
        with Image.open(filename) as image:
            if image.width*image.height > 25_000_000:
                raise ValueError("Image exceeds the OCR pixel limit")
            image.convert("RGB").save(normalized)
        outputs = []
        for psm in (6,11):
            result = subprocess.run([executable,str(normalized),"stdout","--psm",str(psm)],
                                    capture_output=True,text=True,timeout=45,check=True,env=environment)
            outputs.append(result.stdout.upper())
        return "\n".join(outputs)
    if suffix == ".pdf":
        return "\n".join(textextract.pdf_pages(Path(filename)))
    if suffix in {".docx", ".pptx"}:
        # office_parts selects format using the suffix, so use a private alias.
        alias = Path(filename).with_name(Path(filename).name + suffix)
        if not alias.exists():
            shutil.copyfile(filename,alias)
            alias.chmod(0o600)
        return "\n".join(text for _,text in textextract.office_parts(alias))
    if suffix in {".txt", ".csv", ".md", ".html"}:
        return Path(filename).read_text(errors="replace")[:300000]
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**kw):
        return None


def _ed_file(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"edusercontent.com","www.edusercontent.com"} or parsed.username or parsed.port:
        raise ValueError("Only direct Ed attachment URLs are supported")
    directory = DATA_DIR / "attendance" / "evidence"
    ensure_private_dir(directory)
    name = directory / hashlib.sha256(url.encode()).hexdigest()
    if not name.exists():
        with urllib.request.build_opener(_NoRedirect()).open(url,timeout=30) as response:
            raw = response.read(10*1024*1024+1)
        if len(raw)>10*1024*1024:
            raise ValueError("Evidence file exceeds 10 MiB")
        name.write_bytes(raw)
        name.chmod(0o600)
    return name


def collect(records, gmail_allowed=False):
    from . import gmail
    sources, coverage = [], {}
    courses = sorted({row["course"] for row in records})
    if gmail_allowed:
        store = gmail.Store()
        try:
            for course in courses:
                rows = store.con.execute("SELECT id,thread_id,payload,body FROM messages WHERE deleted=0 AND "
                                         "(subject LIKE ? OR body LIKE ?) AND (subject LIKE '%attendance%' OR body LIKE '%attendance%') "
                                         "ORDER BY internal_date DESC LIMIT 15",("%"+course+"%","%"+course+"%")).fetchall()
                for row in rows:
                    url = "https://mail.google.com/mail/#all/"+row["thread_id"]
                    sources.append({"course":course,"text":row["body"],"url":url,"kind":"gmail"})
                    for item in gmail.message_content(json.loads(row["payload"]))["attachments"][:6]:
                        try:
                            saved = gmail.cache_attachment(gmail.Client(),store,row["id"],item["part_id"])
                            text = extract_file(saved["path"],saved["filename"])
                            if text:
                                sources.append({"course":course,"text":course+"\n"+text,"url":url,"kind":"gmail_attachment"})
                        except Exception as e:
                            coverage.setdefault("attachment_errors",[]).append(type(e).__name__)
            coverage["gmail"] = store.status()
        finally:
            store.close()
    else:
        coverage["gmail"] = "Not included: this client lacks Gmail permission"
    if ED_DB.exists():
        con = sqlite3.connect(f"file:{ED_DB.as_posix()}?mode=ro",uri=True)
        con.row_factory = sqlite3.Row
        try:
            for course in courses:
                rows = con.execute("SELECT t.* FROM threads t JOIN courses c ON c.id=t.course_id "
                                   "WHERE (c.code LIKE ? OR c.name LIKE ?) AND (t.title LIKE '%attendance%' OR t.body LIKE '%attendance%') "
                                   "ORDER BY t.updated_at DESC LIMIT 12",("%"+course+"%","%"+course+"%")).fetchall()
                for row in rows:
                    sources.append({"course":course,"text":row["title"]+"\n"+(row["body"] or ""),"url":row["url"],"kind":"ed"})
                    attachments = con.execute("SELECT name,url FROM attachments WHERE thread_id=? LIMIT 8",(row["id"],)).fetchall()
                    for item in attachments:
                        try:
                            filename = _ed_file(item["url"])
                            text = extract_file(filename,item["name"] or urllib.parse.urlsplit(item["url"]).path)
                            if text:
                                sources.append({"course":course,"text":course+"\n"+text,"url":row["url"],"kind":"ed_attachment"})
                        except Exception as e:
                            coverage.setdefault("attachment_errors",[]).append(type(e).__name__)
            coverage["ed"] = "Cached posts and at most eight attachments per matching thread"
        except sqlite3.Error as e:
            coverage["ed"] = type(e).__name__
        finally:
            con.close()
    else:
        coverage["ed"] = "No Ed mirror"
    dbfile = DATA_DIR/"moodle.db"
    if dbfile.exists():
        con = sqlite3.connect(f"file:{dbfile.as_posix()}?mode=ro",uri=True)
        try:
            for course in courses:
                rows = con.execute("SELECT f.url,t.text,f.title FROM content_fts t JOIN content_files f ON f.path=t.path "
                                   "WHERE f.course=? AND (t.text LIKE '%attendance%' OR f.title LIKE '%attendance%') LIMIT 30",(course,)).fetchall()
                for url,text,title in rows:
                    sources.append({"course":course,"text":course+"\n"+title+"\n"+text,"url":url or "moodle-cache", "kind":"moodle"})
            coverage["moodle"] = "Cached indexed content only; image-only Moodle pages may require manual review"
        except sqlite3.Error as e:
            coverage["moodle"] = type(e).__name__
        finally:
            con.close()
    coverage["source_count"] = len(sources)
    coverage["instruction"] = "Evidence collection is bounded, not proof that all sources were checked. OCR candidates require review."
    return {"sources":sources,"coverage":coverage}
