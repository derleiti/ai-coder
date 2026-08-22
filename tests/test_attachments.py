from __future__ import annotations

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from aicoder.attachments import image_attachment, load_path, multimodal_content


class AttachmentExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_source_file_is_loaded_as_bounded_text(self):
        path = self.root / "example.py"
        path.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        item = load_path(path)
        self.assertEqual(item.kind, "text")
        self.assertIn("def hello", item.text)
        self.assertIn("ATTACHMENT: example.py", item.text)

    def test_docx_text_is_extracted_without_office_dependency(self):
        path = self.root / "example.docx"
        xml = b'''<?xml version="1.0"?><w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body></w:document>'''
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("word/document.xml", xml)
        item = load_path(path)
        self.assertIn("Hello DOCX", item.text)

    def test_pptx_text_is_extracted_per_slide(self):
        path = self.root / "slides.pptx"
        xml = b'''<?xml version="1.0"?><p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><a:p><a:r><a:t>Slide text</a:t></a:r></a:p></p:cSld></p:sld>'''
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("ppt/slides/slide1.xml", xml)
        item = load_path(path)
        self.assertIn("[Slide 1]", item.text)
        self.assertIn("Slide text", item.text)

    def test_xlsx_shared_strings_are_extracted(self):
        path = self.root / "sheet.xlsx"
        shared = b'''<?xml version="1.0"?><sst xmlns="urn:s"><si><t>Hello cell</t></si></sst>'''
        sheet = b'''<?xml version="1.0"?><worksheet xmlns="urn:s"><sheetData><row><c t="s"><v>0</v></c><c><v>42</v></c></row></sheetData></worksheet>'''
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("xl/sharedStrings.xml", shared)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        item = load_path(path)
        self.assertIn("Hello cell", item.text)
        self.assertIn("42", item.text)

    def test_pdf_uses_pdftotext_fallback(self):
        path = self.root / "sample.pdf"
        path.write_bytes(b"%PDF-1.4\n")

        def fake_run(args, **kwargs):
            Path(args[-1]).write_text("Extracted PDF text", encoding="utf-8")
            return type("Completed", (), {"returncode": 0, "stderr": ""})()

        with patch("aicoder.attachments.PdfReader", None), patch(
            "aicoder.attachments.shutil.which", return_value="/usr/bin/pdftotext"
        ), patch("aicoder.attachments.subprocess.run", side_effect=fake_run):
            item = load_path(path)
        self.assertIn("Extracted PDF text", item.text)

    def test_image_becomes_data_url_content_block(self):
        raw = b"not-a-real-png-but-bounded"
        item = image_attachment("shot.png", raw)
        content = multimodal_content("find the UI bug", [item])
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        url = content[2]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), raw)


if __name__ == "__main__":
    unittest.main()


class AttachmentSafetyTests(unittest.TestCase):
    def test_fake_image_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.png"
            path.write_bytes(b"this is not an image")
            with self.assertRaisesRegex(ValueError, "image signature"):
                load_path(path)

    def test_archive_member_limit_blocks_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.docx"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("word/document.xml", b"<doc>" + (b"x" * 4096) + b"</doc>")
            with patch("aicoder.attachments.MAX_ZIP_MEMBER_BYTES", 1024):
                with self.assertRaisesRegex(ValueError, "archive member too large"):
                    load_path(path)
