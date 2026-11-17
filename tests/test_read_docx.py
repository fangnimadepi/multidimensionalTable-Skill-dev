"""Check source order, partial-read signaling and local file boundaries."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile


SCRIPT = Path(__file__).resolve().parents[1] / "project-report/scripts/read_docx.py"
NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class ReadDocxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run"
        (self.run / "raw").mkdir(parents=True)

    def write_docx(self, body, name="material.docx"):
        path = self.run / "raw" / name
        xml = f'<w:document xmlns:w="{NAMESPACE}"><w:body>{body}</w:body></w:document>'
        with ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        return path

    def read(self, input_path="raw/material.docx"):
        result = subprocess.run(
            [sys.executable, "-S", str(SCRIPT), "--run-dir", str(self.run),
             "--input", input_path, "--role", "taskbook"],
            capture_output=True, text=True, check=False,
        )
        return result, json.loads(result.stdout)

    def test_preserves_order_wrapped_paragraphs_and_table_merges(self):
        path = self.write_docx(
            '<w:sdt><w:sdtContent><w:p><w:r><w:t>计划要求</w:t></w:r></w:p></w:sdtContent></w:sdt>'
            '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr>'
            '<w:p><w:r><w:t>300 项</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>同批次</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:tc></w:tr></w:tbl><w:p><w:r><w:t>下一步</w:t></w:r></w:p>'
        )
        original = path.read_bytes()
        result, summary = self.read()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(Path(summary["json_path"]).read_text())
        self.assertEqual([row["location"] for row in payload["blocks"]], ["P1", "T1", "P2"])
        cell = payload["blocks"][1]["rows"][0][0]
        self.assertEqual(cell["grid_span"], "2")
        self.assertEqual(cell["vertical_merge"], "restart")
        self.assertEqual(cell["blocks"][0]["text"], "300 项")
        self.assertIn("同批次", Path(summary["text_path"]).read_text())
        self.assertEqual(path.read_bytes(), original)

    def test_image_only_document_is_not_reported_as_readable_text(self):
        self.write_docx('<w:p><w:r><w:drawing/></w:r></w:p>')
        result, summary = self.read()
        self.assertEqual(result.returncode, 3)
        self.assertEqual(summary["text_extraction"], "no_text")
        self.assertEqual(summary["body_objects_to_review"]["drawings_or_images"], 1)

    def test_corrupt_document_reports_parse_failure(self):
        (self.run / "raw/material.docx").write_bytes(b"incomplete download")
        result, summary = self.read()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(summary["text_extraction"], "failed")
        self.assertFalse((self.run / "extracted").exists())

    def test_input_cannot_escape_raw_directory(self):
        (self.root / "outside.docx").write_bytes(b"private")
        result, summary = self.read("raw/../../outside.docx")
        self.assertEqual(result.returncode, 2)
        self.assertIn("超出", summary["error"])

    def test_input_symlink_cannot_escape_run_directory(self):
        outside = self.root / "outside.docx"
        outside.write_bytes(b"private")
        (self.run / "raw/material.docx").symlink_to(outside)
        result, summary = self.read()
        self.assertEqual(result.returncode, 2)
        self.assertIn("超出", summary["error"])

    def test_output_symlink_cannot_escape_run_directory(self):
        self.write_docx('<w:p><w:r><w:t>正文</w:t></w:r></w:p>')
        outside = self.root / "outside"
        outside.mkdir()
        (self.run / "extracted").symlink_to(outside, target_is_directory=True)
        result, summary = self.read()
        self.assertEqual(result.returncode, 2)
        self.assertIn("超出", summary["error"])
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
