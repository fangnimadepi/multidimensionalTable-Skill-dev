"""Cover self-contained engine files, card fact checks and readable sources."""

import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "project-report/scripts"))
from runtime_support import prepare_runtime


class RuntimePackagingTests(unittest.TestCase):
    def test_materialized_engine_is_user_local_and_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            engine = prepare_runtime(workspace)
            self.assertTrue(engine.is_relative_to(workspace))
            self.assertTrue((engine / "scripts/report_worker.py").is_file())
            self.assertTrue((engine / "config/review_examples_v0.2.json").is_file())
            self.assertEqual(engine, prepare_runtime(workspace))
            (engine / "scripts/report_worker.py").write_text("changed")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                prepare_runtime(workspace)


@unittest.skipUnless(importlib.util.find_spec("docx") and importlib.util.find_spec("pypdf"), "Needs initialized document environment")
class WordOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "project-report/runtime/scripts"))

    def test_source_locations_survive_without_nonfunctional_navigation(self):
        from word_reports import render_docx, validate_docx

        text = '# 合成审查报告\n\n[查看本项依据](#item-1)\n\n来源见[来源01](#item-1)。\n\n## 附页 关键依据\n\n| 事项 | 位置 |\n| --- | --- |\n| <a id="item-1"></a>工具数量 | 任务书第2表第1行；报告原PDF第5页 |\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.docx"
            stats = render_docx(text, path, "合成审查报告")
            self.assertEqual(stats["internal_links"], 0)
            self.assertEqual(stats["bookmarks"], 0)
            self.assertEqual(validate_docx(path)["tables"], 1)
            with ZipFile(path) as archive:
                from lxml import etree
                xml = etree.fromstring(archive.read("word/document.xml"))
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                text = "".join(xml.xpath('//w:t/text()', namespaces=ns))
                for value in ("工具数量", "任务书第2表第1行；报告原PDF第5页", "来源见来源01。"):
                    self.assertIn(value, text)
                self.assertNotIn("查看本项依据", text)
                self.assertFalse(xml.xpath('//w:hyperlink|//w:bookmarkStart', namespaces=ns))
                self.assertTrue(xml.xpath('//w:p[w:pPr/w:pStyle[@w:val="Title"]]/w:r/w:rPr/w:color[@w:val="000000"]', namespaces=ns))
                self.assertFalse(xml.xpath('//w:pPr/w:pBdr', namespaces=ns))

    def test_visual_emphasis_preserves_complete_qualifications(self):
        from reading_copy import emphasize_lead, emphasize_review_summary

        text = '测试样本的97.1%达到R²>0.98，但尚未进入企业生产线部署。后续需开展联合验证。'
        emphasized = emphasize_lead(text)
        self.assertTrue(emphasized.startswith('**测试样本'))
        self.assertIn('但尚未进入企业生产线部署。**', emphasized)
        self.assertEqual(emphasized.replace('**', ''), text)
        summary = '### 综合审查意见\n\n' + text + '\n\n### 与 PI 老师确认事项\n\n' + text + '\n'
        actual = emphasize_review_summary(summary)
        self.assertEqual(actual.replace('**', ''), summary)
        self.assertNotIn('**', actual.split('### 与 PI 老师确认事项')[1])
        self.assertEqual(emphasize_lead('**已有加粗**的段落不再嵌套处理。'), '**已有加粗**的段落不再嵌套处理。')

    def test_algorithm_star_inside_bold_text_is_preserved_without_markup(self):
        from docx import Document
        from word_reports import text_runs

        document = Document()
        paragraph = document.add_paragraph()
        text_runs(paragraph, '**已形成AOT* v1.0，但尚未完成集成。**后续继续验证。')
        self.assertEqual(paragraph.text, '已形成AOT* v1.0，但尚未完成集成。后续继续验证。')
        self.assertTrue(any('AOT*' in run.text and run.bold for run in paragraph.runs))

    def test_card_numbers_participate_in_the_same_fact_checks_as_the_body(self):
        import achievement_expression as expression

        paragraph = {"text": "已形成12项工具。", "kind": "事实归纳", "fact_ids": ["F01"]}
        raw = {"narrative_format": "reader-questions-v2", "value_title": "让工具进入研究任务", "title_fact_ids": ["F01"],
               "fact_sheet": {key: copy.deepcopy(paragraph) for key in expression.QUESTIONS},
               "short_version": [copy.deepcopy(paragraph)], "long_intro": [copy.deepcopy(paragraph)],
               "long_sections": [{"title": "工具如何帮助研究", "paragraphs": [copy.deepcopy(paragraph)]} for _ in range(4)],
               "questions": [], "card": {key: copy.deepcopy(paragraph) for key in ("delivered", "method", "value", "stage", "next")}}
        facts = [{"fact_id": "F01", "text": "已形成12项工具。", "citations": [{"quote": "已形成12项工具。"}]}]
        draft, errors = expression.normalize(raw, facts)
        self.assertFalse(errors)
        self.assertEqual({value["statement_id"] for value in expression.statements(draft) if value["statement_id"].startswith("G")},
                         {"G01", "G02", "G03", "G04", "G05"})
        raw["card"]["delivered"]["text"] = "已形成99项工具。"
        _, errors = expression.normalize(raw, facts)
        self.assertIn({"statement_id": "G01", "reason": "project_number_without_source"}, errors)


if __name__ == "__main__":
    unittest.main()
