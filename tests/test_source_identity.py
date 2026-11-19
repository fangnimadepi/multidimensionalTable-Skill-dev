"""Conclusion replacement must not change the identity of the same raw block."""

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-report/runtime/scripts"))
import project_report_evidence as evidence
import project_report_findings as findings
import project_report_synthesis as synthesis


class SourceIdentityTests(unittest.TestCase):
    def fixture(self):
        raw = "框架已经建成。模型验证仍在推进。"
        short = "框架已经建成。"
        cite = {"side": "report", "ref": "R-P1", "quote": short, "role": "current"}
        report = {
            "project_profile": {"project_name": "样例"}, "plan_items": [], "alignments": [],
            "review_scope": {"plan_file": "计划.docx", "report_file": "总结.docx"},
            "overall_assessment": {"statements": [{"section": "conclusion", "text": short, "citations": [cite]}]},
            "source_index": [{"source_id": "依据01", "side": "report", "ref": "R-P1", "quote": short,
                              "location": "段落1", "file_name": "总结.docx"}],
            "presentation": {"categorized_findings": {"items": []}, "statements": []}, "validation": {},
        }
        return report, {"plan_blocks": {}}, {"R-P1": {"text": raw, "role": "actual_or_body"}}

    @patch.object(findings.p, "review_topics", return_value=([], []))
    def test_short_excerpt_and_full_block_keep_the_same_id_through_final_validation(self, topics):
        report, source, records = self.fixture()
        context = synthesis.context_for(report, source, records)
        self.assertEqual(context["sources"][0]["source_id"], "依据01")
        draft = synthesis.resolve({"paragraphs": [{"text": "框架已经建成，模型验证仍在推进。", "source_ids": ["依据01"],
                                                 "has_assessment": False, "item_ids": [], "issue_ids": []}]}, context)
        audit = {"checks": [{"paragraph_id": "CS01", "supported": True}], "state_checks": [],
                 "module_check": dict.fromkeys(("reader_friendly", "not_repetitive", "scope_preserved", "states_consistent"), True)}
        result, errors = synthesis.validate(draft, audit, context)
        self.assertEqual(errors, [])
        synthesis.attach(report, result, [])
        self.assertEqual([entry["source_id"] for entry in report["source_index"]], ["依据01", "依据02"])
        groups, uses = evidence.context_for(report, source, records)
        self.assertEqual(groups[0]["source_id"], "依据01")
        self.assertEqual(groups[0]["alias_ids"], ["依据02"])
        self.assertEqual(uses[0]["source_ids"], ["依据01"])
        report["presentation"]["evidence_notes"] = {"items": [
            {**groups[0], "quote": records["R-P1"]["text"], "semantic_reviewed": True}]}
        final_context = synthesis.context_for(report, source, records)
        _, errors = synthesis.validate(result, audit, final_context)
        self.assertEqual(errors, [])
        altered = copy.deepcopy(result)
        altered["paragraphs"][0]["source_ids"] = ["不存在的依据"]
        _, errors = synthesis.validate(altered, audit, final_context)
        self.assertIn("conclusion_source_not_allowed:CS01", errors)

    @patch.object(findings.p, "review_topics", return_value=([], []))
    def test_an_id_from_a_different_block_cannot_be_preferred(self, topics):
        report, source, records = self.fixture()
        report["presentation"]["evidence_notes"] = {"items": [
            {"source_id": "依据01", "side": "report", "ref": "R-P2"}]}
        with self.assertRaisesRegex(ValueError, "identity"):
            findings.evidence_context(report, source, records)


if __name__ == "__main__":
    unittest.main()
