"""Prevent role mixups and publication before the inherited checks pass."""

import sys
import json
from contextlib import redirect_stdout
from io import StringIO
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "project-report"
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "runtime/scripts")]
from local_reports import checked_reports, exclusive_work, source_path, worker_failure


class LocalReportsTests(unittest.TestCase):
    def test_direct_doctor_does_not_prepare_an_engine_or_require_existing_config(self):
        import local_reports
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "missing/config.json"
            output = StringIO()
            with (patch.object(sys, "argv", ["local_reports.py", "--config", str(file), "doctor"]),
                  patch("bootstrap.diagnose", return_value={"status": "initialization_required", "read_only": True}),
                  patch.object(local_reports, "setup_runtime") as prepare,
                  redirect_stdout(output)):
                self.assertEqual(local_reports.main(), 0)
            self.assertTrue(json.loads(output.getvalue())["read_only"])
            self.assertFalse(file.parent.exists())
            prepare.assert_not_called()

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

    def test_balance_failure_remains_actionable_after_worker_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "worker-failure.json").write_text(json.dumps({
                "type": "ModelRequestError", "model_category": "balance", "http_status": 402,
                "message": "safe HTTP 402", "retryable": False}), encoding="utf-8")
            failure = worker_failure(root, subprocess.CalledProcessError(1, "worker"))
            self.assertEqual(failure.error_code, "model_balance")
            self.assertFalse(failure.retryable)
            self.assertIn("充值", failure.details()["next_action"])

    def test_plain_text_pdf_does_not_install_ocr_and_scanned_pdf_does(self):
        import local_reports
        with (patch("pdf_ocr.inspect_pdf", return_value=(["native text"], [])),
              patch("bootstrap.initialize") as initialize):
            local_reports.ensure_pdf_ocr(Path("synthetic.pdf"))
            initialize.assert_not_called()
        with (patch("pdf_ocr.inspect_pdf", return_value=([""], [0])),
              patch("pdf_ocr.backend_name", return_value="rapidocr"),
              patch("pdf_ocr.capabilities", side_effect=[{"available": False}, {"available": True}]),
              patch.object(sys, "prefix", "/synthetic/.jingyan/venv"),
              patch.object(sys, "base_prefix", "/synthetic/python"), patch("bootstrap.initialize") as initialize):
            local_reports.ensure_pdf_ocr(Path("synthetic.pdf"))
            initialize.assert_called_once_with(Path("/synthetic/.jingyan"), profile="ocr")

    def test_worker_preserves_the_actual_validation_stage_and_feedback_file(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-report/runtime/scripts"))
        import report_worker
        from project_report_contract import ReportValidationError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            attempt = root / "attempt-one"
            attempt.mkdir()
            feedback = root / "review/failed-grouped/project-final.json"
            feedback.parent.mkdir(parents=True)
            feedback.write_text('{"feedback":{"conclusion":["unsupported_whole_project_conclusion"]}}')
            manifest = attempt / "manifest.json"
            manifest.write_text("{}")
            error = ReportValidationError("Grouped review final composition did not pass checks",
                                          stage="review/final", feedback_file=feedback)
            with (patch.object(sys, "argv", ["report_worker.py", str(manifest)]),
                  patch.object(report_worker, "run", side_effect=error), self.assertRaises(SystemExit)):
                report_worker.main()
            failure = worker_failure(attempt, subprocess.CalledProcessError(1, "worker"))
            self.assertEqual(failure.details()["validation_stage"], "review/final")
            self.assertEqual(failure.details()["validation_file"], str(feedback))
            self.assertIn("履约报告最终", str(failure))
            self.assertFalse(failure.retryable)

    def test_artifact_failure_returns_the_exact_final_check_file(self):
        import report_worker
        from project_report_contract import ReportValidationError
        with tempfile.TemporaryDirectory() as tmp:
            attempt = Path(tmp) / "attempt"
            attempt.mkdir()
            feedback = attempt / "artifact-errors.json"
            feedback.write_text('{"review":["integrated_conclusion:conclusion_source_not_allowed:CS02"]}')
            manifest = attempt / "manifest.json"
            manifest.write_text("{}")
            error = ReportValidationError("source binding check failed", stage="review/artifacts", feedback_file=feedback)
            with (patch.object(sys, "argv", ["report_worker.py", str(manifest)]),
                  patch.object(report_worker, "run", side_effect=error), self.assertRaises(SystemExit)):
                report_worker.main()
            failure = worker_failure(attempt, subprocess.CalledProcessError(1, "worker"))
            self.assertEqual(failure.details()["validation_stage"], "review/artifacts")
            self.assertEqual(failure.details()["validation_file"], str(feedback))
            self.assertFalse(failure.retryable)


if __name__ == "__main__":
    unittest.main()
