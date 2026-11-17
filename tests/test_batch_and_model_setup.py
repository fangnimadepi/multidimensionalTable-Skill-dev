"""Cover missing-material routing and per-user key precedence."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-report/scripts"))
from model_setup import load_credentials, read_credentials
from plan_batch import make_plan, route


class BatchRoutingTests(unittest.TestCase):
    def test_ninety_rows_are_retained_in_groups_and_routed_independently(self):
        records = [{"record_id": f"record-{i}", "project_name": f"项目{i}",
                    "taskbook": [{"name": "任务书.docx"}],
                    "report": [{"name": "中期总结.pdf"}] if i % 2 else []} for i in range(90)]
        inventory = {"base_token": "base", "table_id": "table", "fields": {}, "records": records}
        plan = make_plan(inventory, ["review", "achievement"], 5)
        self.assertEqual(plan["total"], 90)
        self.assertEqual(plan["counts"], {"pending": 90})
        self.assertEqual(plan["records"][-1]["group"], 18)
        self.assertEqual(sum(entry["types"] == ["forecast"] for entry in plan["records"]), 45)
        records.append(records[0])
        with self.assertRaisesRegex(ValueError, "重复"):
            make_plan(inventory, ["review", "achievement"], 5)

    def test_unreadable_report_cannot_become_taskbook_only_forecast(self):
        record = {"record_id": "p", "taskbook": [{"name": "任务书.docx"}],
                  "report": [], "input_error": "总结报告附件读取被拒绝"}
        result = route(record, ["review", "achievement"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["types"], [])

    def test_review_only_and_version_ambiguity_do_not_generate_an_unrequested_forecast(self):
        record = {"record_id": "p", "taskbook": [{"name": "任务书.docx"}], "report": []}
        self.assertEqual(route(record, ["review"])["status"], "blocked")
        record["report"] = [{"name": "旧总结.docx"}, {"name": "新总结.docx"}]
        self.assertEqual(route(record, ["review", "achievement"])["status"], "needs_selection")


class CredentialIsolationTests(unittest.TestCase):
    def test_explicit_missing_key_does_not_fall_back_to_shared_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key_file = root / "model.env"
            key_file.write_text("SILICONFLOW_API_KEY=example\n")
            key_file.chmod(0o600)
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "shared-example"}):
                with self.assertRaisesRegex(ValueError, "缺少当前平台"):
                    load_credentials(root / "config.json", {"credentials_file": "model.env", "provider": "deepseek"},
                                     "DEEPSEEK_API_KEY")

    @unittest.skipIf(os.name == "nt", "POSIX file mode check")
    def test_world_readable_key_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.env"
            path.write_text("DEEPSEEK_API_KEY=example\n")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "仅当前用户"):
                read_credentials(path)


if __name__ == "__main__":
    unittest.main()
