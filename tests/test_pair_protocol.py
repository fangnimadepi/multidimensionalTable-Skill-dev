"""Missing paired verdicts may be checked separately; denials stay binding."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-report/runtime/scripts"))
from project_report_grouped import complete_missing_pair_check


class PairProtocolTests(unittest.TestCase):
    def test_missing_field_is_checked_against_the_same_reports_only(self):
        original = {"module": [True, False, True, True], "evidence": [{"flags": [False]}]}
        pair = {"paired_review": "已验证8B模型，32B仍待验证。", "achievement_draft": {"text": "完成8B验证。"}}
        calls = []

        def call(stage, prompt, payload, budget):
            calls.append((stage, payload))
            return {"paired_check": {"consistent": True, "issues": []}, "module": [True] * 4}

        result, stage = complete_missing_pair_check(call, "audit", original, pair)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["extra_modules"]["paired_reports"], pair)
        self.assertEqual(stage, calls[0][0])
        self.assertEqual(result["module"], original["module"])
        self.assertEqual(result["evidence"], original["evidence"])
        self.assertNotIn("paired_check", original)

    def test_explicit_conflict_is_not_retried_or_overridden(self):
        original = {"paired_check": {"consistent": False, "issues": [{"reason": "8B与32B混淆"}]}}

        def call(*args):
            self.fail("An explicit negative verdict must not be retried as a missing field")

        result, stage = complete_missing_pair_check(call, "audit", original, {"paired_review": "原文"})
        self.assertIs(result, original)
        self.assertEqual(stage, "audit")

    def test_absent_supplemental_verdict_stays_unverified(self):
        result, _ = complete_missing_pair_check(lambda *args: {}, "audit", {}, {"paired_review": "原文"})
        self.assertIsNone(result["paired_check"])


if __name__ == "__main__":
    unittest.main()
