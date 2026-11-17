"""Prevent role mixups and publication before the inherited checks pass."""

import sys
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-report" / "scripts"))
from local_reports import checked_reports, exclusive_work, source_path, worker_failure


class LocalReportsTests(unittest.TestCase):
    def test_rejects_other_role_and_escaping_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for role in ("taskbook", "report"):
                (root / "raw" / role).mkdir(parents=True)
            source = root / "raw/report/source.docx"
            source.write_bytes(b"material")
            (root / "raw/taskbook/source.docx").symlink_to(source)
            with self.assertRaisesRegex(ValueError, "超出"):
                source_path(root, "raw/taskbook/source.docx", "taskbook", {})
            self.assertEqual(source_path(root, "raw/report/source.docx", "report", {}), source.resolve())

    def test_publication_requires_paired_check_and_current_materials(self):
        types = ["review", "achievement"]
        hashes = {"plan": "taskbook", "report": "summary"}
        record = {"reports": dict.fromkeys(types, "checked markdown"), "materialHashes": hashes,
                  "pipelineFingerprint": "rules", "checks": {kind: {"passed": True} for kind in types}}
        with self.assertRaises(ValueError):
            checked_reports(record, types, hashes, "rules")
        record["checks"]["paired"] = {"passed": True}
        with self.assertRaises(ValueError):
            checked_reports(record, types, {**hashes, "report": "changed"}, "rules")
        self.assertEqual(checked_reports(record, types, hashes, "rules"), record["reports"])

    def test_one_worker_per_project_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            with exclusive_work(work):
                with self.assertRaisesRegex(ValueError, "已有生成任务"), exclusive_work(work):
                    self.fail("A second worker acquired the same context")
            with exclusive_work(work):
                self.assertTrue(work.is_dir())

    def test_validation_failure_is_returned_without_raw_sources_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "worker-failure.json").write_text(json.dumps({
                "type": "ValueError", "message": "Grouped review initial composition did not pass checks",
                "untrusted_detail": "synthetic-private-value"}), encoding="utf-8")
            failure = worker_failure(root, subprocess.CalledProcessError(1, "worker"))
            self.assertFalse(failure.details()["retryable"])
            self.assertEqual(failure.details()["error_code"], "report_validation_failed")
            self.assertNotIn("synthetic-private-value", str(failure.details()))

    def test_temporary_model_connection_failure_can_be_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "worker-failure.json").write_text(json.dumps({
                "type": "RuntimeError", "message": "deepseek 模型调用未完成（HTTP 503）"}), encoding="utf-8")
            failure = worker_failure(root, subprocess.CalledProcessError(1, "worker"))
            self.assertTrue(failure.details()["retryable"])
            self.assertEqual(failure.error_code, "model_connection_failed")


if __name__ == "__main__":
    unittest.main()
