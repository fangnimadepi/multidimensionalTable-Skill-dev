"""Explicitly withheld completion opinions must not become positive claims."""

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-report/runtime/scripts"))
from project_report_synthesis import validate


class ConclusionClaimTests(unittest.TestCase):
    def errors_for(self, text):
        paragraph = {"paragraph_id": "CS01", "text": text, "has_assessment": True,
                     "item_ids": [], "issue_ids": [], "source_ids": ["plan", "report"],
                     "citations": [{"side": "plan", "role": "target", "quote": text},
                                   {"side": "report", "role": "current", "quote": text}]}
        context = {"editorial_policy": "advisory", "established_reported_results": [], "plan_items": [],
                   "allowed_issue_ids": [], "sources": [{"source_id": "plan"}, {"source_id": "report"}],
                   "pending_questions": [], "checked_alignments": []}
        audit = {"checks": [{"paragraph_id": "CS01", "supported": True}], "state_checks": [],
                 "module_check": dict.fromkeys(("reader_friendly", "not_repetitive", "scope_preserved",
                                                "states_consistent"), True)}
        draft = {"paragraphs": [paragraph]}
        original = copy.deepcopy(draft)
        _, errors = validate(draft, audit, context)
        self.assertEqual(draft, original)
        return errors

    def test_withheld_completion_and_explicit_unfinished_status_are_allowed(self):
        for text in ("相关系数指标尚无当前测试结果，不能据此认定本期节点全部完成。",
                     "目前尚不能确认所有中期目标已经完成。", "本期全部任务尚未完成。",
                     "现有结果并非全部任务均已完成，仍有指标需要确认。",
                     "当前达成情况与“中期节点指标全部完成”的总体表述之间存在需要核实的边界。"):
            with self.subTest(text=text):
                self.assertNotIn("unsupported_whole_project_conclusion", self.errors_for(text))

    def test_positive_claims_remain_blocked_including_after_a_negative_clause(self):
        for text in ("本期节点全部完成。", "所有中期目标已经完成。", "各项任务均已完成。",
                     "不能据此认定本期节点全部完成，但所有目标已经完成。",
                     "不能据此认定本期节点全部完成；项目已通过验收。",
                     "与“中期节点指标全部完成”的表述之间存在疑问，但所有任务均已完成。",
                     "项目已经实现“中期节点指标全部完成”。"):
            with self.subTest(text=text):
                self.assertIn("unsupported_whole_project_conclusion", self.errors_for(text))


if __name__ == "__main__":
    unittest.main()
