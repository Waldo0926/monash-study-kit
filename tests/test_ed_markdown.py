import sqlite3
import unittest

from monash_study_kit import edlib


def doc(inner: str) -> str:
    return f'<document version="2.0">{inner}</document>'


class ContentToMarkdownTests(unittest.TestCase):
    """Ed 的 document 字段是纯文本，会丢掉链接网址和附件；必须从 content 的 XML 转。"""

    def test_link_keeps_its_url(self):
        md = edlib.doc_to_text(doc('<paragraph>See <link href="https://x.org/a?b=1&amp;c=2">the sheet</link>.</paragraph>'))
        self.assertEqual(md, "See [the sheet](https://x.org/a?b=1&c=2).")

    def test_bare_link_is_not_doubled(self):
        md = edlib.doc_to_text(doc('<paragraph><link href="https://x.org">https://x.org</link></paragraph>'))
        self.assertEqual(md, "https://x.org")

    def test_inline_marks_and_headings(self):
        md = edlib.doc_to_text(doc('<heading level="2">Plan</heading>'
                                   '<paragraph><bold>Due </bold>Friday, run <code>npm test</code></paragraph>'))
        self.assertEqual(md, "## Plan\n\n**Due** Friday, run `npm test`")

    def test_numbered_list_with_nested_bullets(self):
        md = edlib.doc_to_text(doc(
            '<list style="number"><list-item><paragraph>one</paragraph>'
            '<list style="bullet"><list-item><paragraph>a</paragraph></list-item></list></list-item>'
            '<list-item><paragraph>two</paragraph></list-item></list>'))
        self.assertEqual(md, "1. one\n   - a\n2. two")

    def test_snippet_becomes_fenced_code(self):
        md = edlib.doc_to_text(doc('<snippet language="python"><snippet-file id="code">x = 1\nprint(x)</snippet-file></snippet>'))
        self.assertEqual(md, "```python\nx = 1\nprint(x)\n```")

    def test_callout_is_quoted(self):
        md = edlib.doc_to_text(doc('<callout type="warning"><bold>No late work</bold></callout>'))
        self.assertEqual(md, "> **No late work**")

    def test_files_images_and_videos(self):
        content = doc('<file url="https://s/f1" filename="spec.pdf"/><figure><image src="https://s/i1"/></figure>'
                      '<video src="https://v/1?id=2&amp;t=0"/>')
        md = edlib.doc_to_text(content)
        self.assertIn("[📎 spec.pdf](https://s/f1)", md)
        self.assertIn("![图片](https://s/i1)", md)
        self.assertIn("[视频](https://v/1?id=2&t=0)", md)
        self.assertEqual([(a["kind"], a["name"], a["url"]) for a in edlib.doc_attachments(content)],
                         [("file", "spec.pdf", "https://s/f1"), ("image", "i1", "https://s/i1"),
                          ("video", "1?id=2&t=0", "https://v/1?id=2&t=0")])

    def test_plain_text_passes_through(self):
        self.assertEqual(edlib.doc_to_text("  just text \n"), "just text")

    def test_body_prefers_content_over_document(self):
        obj = {"document": "the sheet", "content": doc('<paragraph><link href="https://x">the sheet</link></paragraph>')}
        self.assertEqual(edlib.body_of(obj), "[the sheet](https://x)")

    def test_broken_xml_still_keeps_urls(self):
        md = edlib.doc_to_text('<document><paragraph><link href="https://x">t</link><bold></paragraph>')
        self.assertIn("[t](https://x)", md)

    def test_attachments_saved_for_thread_and_replies(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(edlib.SCHEMA)
        thread = {"content": doc('<file url="https://s/a" filename="a.pdf"/>'),
                  "answers": [{"id": 9, "content": doc('<image src="https://s/b"/>')}]}
        replies = edlib.flatten_replies(thread)
        edlib.save_attachments(conn, 1, thread, replies)
        edlib.save_attachments(conn, 1, thread, replies)   # 重抓不重复
        self.assertEqual(conn.execute("SELECT reply_id, kind, name FROM attachments ORDER BY reply_id").fetchall(),
                         [(0, "file", "a.pdf"), (9, "image", "b")])


if __name__ == "__main__":
    unittest.main()
