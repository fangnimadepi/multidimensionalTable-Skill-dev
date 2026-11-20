"""Exercise host-model continuations with synthetic replies and no API access."""

from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "project-report"
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "runtime/scripts")]
import host_reports as host
from host_protocol import HostTaskPending, HostReportFailure
from host_content import validate_draft, validate_facts
from host_compose import validate as validate_compose
from host_materials import batches
from host_fixtures import documents, reply, draft_reply, ACTUAL
import local_reports


class HostWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "project"
        for role, content in zip(("taskbook", "report"), documents()):
            file = self.run / "raw" / role / (role + ".docx")
            file.parent.mkdir(parents=True)
            file.write_bytes(content)
        self.args = SimpleNamespace(types=["review", "achievement"], taskbook="raw/taskbook/taskbook.docx",
                                    report="raw/report/report.docx", record_id="recNative", project_name="样例项目", response=None)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for target in ("llm_client.complete_json", "local_reports.setup_runtime", "workbuddy_models.read_models",
                       "model_setup.manual_model_options"):
            self.guards.enter_context(patch(target, side_effect=AssertionError("Host mode accessed model API/config")))

    def next(self):
        try:
            return "checked", host.advance(self.args, ROOT / "runtime", self.run, {})
        except HostTaskPending as pending:
            request = json.loads(Path(pending.task["request_file"]).read_text())
            self.args.response = Path(pending.task["response_file"])
            return request["stage"], request

    def respond(self, request, body=None):
        self.args.response.write_text(json.dumps(reply(request) if body is None else
                                                {"request_id": request["request_id"], "result": body}, ensure_ascii=False))

    def complete(self):
        stages = []
        for _ in range(30):
            stage, value = self.next()
            stages.append(stage)
            if stage == "checked":
                return value, stages
            self.respond(value)
        self.fail("Host workflow did not terminate")

    def test_two_editable_reports_complete_without_any_model_configuration(self):
        receipt, stages = self.complete()
        self.assertEqual(stages, ["compose", "check", "checked"])
        self.assertEqual(set(receipt["documents"]), {"review", "achievement"})
        self.assertEqual(receipt["execution_mode"], "host")
        self.assertEqual(receipt["direct_api_calls"], 0)
        self.assertIn("非独立", receipt["review_method"])
        from docx import Document
        review = receipt["documents"]["review"]
        self.assertIn("计划与进展对照", Path(review["markdown"]).read_text())
        self.assertIn("想和您确认一下", Path(review["markdown"]).read_text())
        self.assertTrue(Document(review["docx"]).tables)
        for document in receipt["documents"].values():
            self.assertEqual(host.file_hash(Path(document["docx"])), document["sha256"])
        # Reopening resumes the completed output, with no additional host task.
        self.assertEqual(self.next()[1], receipt)

    def test_taskbook_only_routes_to_forecast_without_claiming_current_results(self):
        self.args.types, self.args.report = ["forecast"], None
        receipt, stages = self.complete()
        self.assertEqual(set(receipt["documents"]), {"forecast"})
        text = Path(receipt["documents"]["forecast"]["markdown"]).read_text()
        for heading in ("目标成果提炼", "项目概述", "简版", "科普长版", "后续验证"):
            self.assertIn(heading, text)
        self.assertIn("任务书版预期成果", text)
        self.assertNotIn(ACTUAL, text)
        self.assertNotIn("审查阶段与基准", text)

    def material(self):
        return json.loads(next((self.run / "_host").glob("*/materials.json")).read_text())

    def patch_response(self, request, patches):
        self.args.response.write_text(json.dumps({"request_id": request["request_id"], "patches": patches}, ensure_ascii=False))

    def test_bad_literal_citations_need_field_repair_before_export(self):
        _, request = self.next()
        result = reply(request)["result"]
        result["plans"][0]["sources"] = ["A9999"]
        self.respond(request, result)
        _, repair = self.next()
        self.assertEqual(repair["stage"], "repair")
        self.assertIn("revision_feedback", repair)
        self.assertEqual(repair["allowed_roots"], ["/plans"])
        self.assertNotIn("review", repair)
        self.respond(repair)
        _, check = self.next()
        self.respond(check)
        self.assertEqual(self.next()[0], "checked")

    def test_missing_review_is_not_accepted_as_successful_paired_delivery(self):
        _, request = self.next()
        result = reply(request)["result"]
        result.pop("review")
        self.respond(request, result)
        _, repair = self.next()
        self.assertEqual(repair["stage"], "repair")
        self.assertIn("全部报告类型", str(repair["revision_feedback"]))
        self.respond(repair)
        _, check = self.next()
        self.respond(check)
        self.assertEqual(set(self.next()[1]["documents"]), {"review", "achievement"})

    def negative_check(self):
        _, compose = self.next()
        self.respond(compose)
        _, check = self.next()
        negative = reply(check)["result"]
        negative["issues"] = [{"path": "review/conclusion/0", "reason": "此句应保留验证范围"}]
        self.respond(check, negative)
        return compose, self.next()[1]

    def test_explicit_negative_review_requires_changed_draft_not_a_positive_recheck(self):
        compose, repair = self.negative_check()
        self.assertEqual(repair["stage"], "repair")
        self.patch_response(repair, [{"path": "/review/conclusion/0", "value": reply(compose)["result"]["review"]["conclusion"][0]}])
        with self.assertRaises(HostReportFailure):
            self.next()
        self.assertFalse((self.run / "reports").exists())

    def test_repaired_draft_can_pass_and_keeps_negative_feedback_for_diagnostics(self):
        compose, repair = self.negative_check()
        original = reply(compose)["result"]["review"]["conclusion"][0]
        self.patch_response(repair, [{"path": "/review/conclusion/0", "value": original + "后续仍需结合验证结果明确完成范围。"}])
        _, check = self.next()
        self.respond(check)
        receipt = self.next()[1]
        self.assertTrue(list(Path(receipt["diagnostics"]).glob("feedback-*.json")))
        self.assertTrue(receipt["checks"]["paired"]["passed"])

    def test_actual_numbers_cannot_borrow_the_planned_value(self):
        _, request = self.next()
        result = reply(request)["result"]
        result["review"]["progress_summary"][0] = "已完成32B模型验证。"
        errors = validate_compose(result, self.material(), self.args.types)
        self.assertTrue(any("实际成果数字" in value for value in errors))
        result["key_facts"][0]["text"] = "已完成32B模型验证。"
        errors = validate_compose(result, self.material(), self.args.types)
        self.assertTrue(any("关键数字或规格" in value for value in errors))

    def test_withheld_completion_is_allowed_but_positive_whole_project_claim_is_not(self):
        _, request = self.next()
        result = reply(request)["result"]
        result["review"]["conclusion"][0] = "不能据此认定本期节点全部完成。"
        self.assertFalse(validate_compose(result, self.material(), self.args.types))
        result["review"]["conclusion"][0] = "所有中期目标已经完成。"
        self.assertTrue(any("整体履约认定" in error for error in validate_compose(result, self.material(), self.args.types)))

    def test_pending_request_and_input_tampering_are_rejected(self):
        _, request = self.next()
        file = next((self.run / "_host").glob("*/request-*.json"))
        saved = file.read_text()
        file.write_text(saved.replace("32000", "32001") + " ")
        # Whitespace is irrelevant, so change a meaningful task field.
        value = json.loads(file.read_text())
        value["project_name"] = "另一个项目"
        file.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.next()
        file.write_text(saved)
        original_response = self.args.response
        self.args.response = self.root / "outside.json"
        with self.assertRaisesRegex(ValueError, "超出"):
            self.next()
        self.args.response = original_response
        self.respond(request)
        # A changed raw input cannot consume a response from the previous job.
        path = self.run / self.args.report
        changed = __import__("docx").Document(path)
        changed.add_paragraph("新增进展信息。")
        changed.save(path)
        with self.assertRaises(ValueError):
            self.next()

    def test_source_batching_covers_all_units_without_truncation(self):
        sources = [{"source_id": str(i), "text": "测试材料" * 1000} for i in range(100)]
        chunks = list(batches(sources))
        self.assertGreater(len(chunks), 1)
        self.assertEqual([value for chunk in chunks for value in chunk], sources)

    def test_short_whole_date_cell_is_valid_but_a_tiny_detached_excerpt_is_not(self):
        sources = [{"source_id": "date", "side": "plan", "text": "6月", "plan_candidate": True, "role": "body"},
                   {"source_id": "task", "side": "plan", "text": "完成模型验证。", "plan_candidate": True, "role": "body"}]
        raw = {"plan_items": [{"title": "模型验证", "requirement": "完成模型验证。", "node": "6月", "scope": "本期",
                              "citations": [{"source_id": "date", "quote": "6月"}, {"source_id": "task", "quote": "完成模型验证。"}]}],
               "facts": [], "coverage": [{"source_id": "date", "disposition": "not_requirement", "reason": "时间标签"},
                                        {"source_id": "task", "disposition": "requirements", "reason": ""}]}
        _, errors = validate_facts(raw, sources)
        self.assertFalse(errors)
        sources[0]["text"] = "计划在6月以前完成，具体阶段范围另行说明。"
        _, errors = validate_facts(raw, sources)
        self.assertTrue(any("摘录不是原文" in error for error in errors))

    def test_editorial_advice_does_not_reject_source_supported_reports(self):
        _, request = self.next()
        self.respond(request)
        _, request = self.next()
        answer = reply(request)["result"]
        answer.update(editorial_notes=["可进一步减少重复介绍。"])
        self.respond(request, answer)
        self.assertEqual(self.next()[0], "checked")

    def test_checked_host_report_can_be_reformatted_without_api_or_ocr(self):
        receipt, _ = self.complete()
        original = Path(receipt["receipt"]).read_bytes()
        config = self.root / "config.json"
        config.write_text(json.dumps({"workspace_root": str(self.root)}))
        from reexport_checked import reexport
        args = SimpleNamespace(config=config, run_dir=self.run, receipt=Path(receipt["receipt"]),
                               taskbook=self.args.taskbook, report=self.args.report)
        with patch.object(local_reports, "read_material", side_effect=AssertionError("OCR/parse on reexport")):
            exported = reexport(args)
        self.assertTrue(exported["export_only"])
        self.assertEqual(exported["host_steps_completed"], 0)
        self.assertEqual(set(exported["documents"]), {"review", "achievement"})
        self.assertEqual(Path(receipt["receipt"]).read_bytes(), original)

    def test_host_runtime_uses_portable_ocr_without_model_settings(self):
        with (patch("runtime_support.prepare_runtime", return_value=ROOT / "runtime"),
              patch.dict(os.environ)):
            _, model = local_reports.setup_host_runtime(self.root / "config.json", {"report_runtime": "invalid legacy API settings"})
            self.assertEqual(os.environ["PROJECT_ASSISTANT_OCR_ENGINE"], "rapidocr")
            self.assertEqual(model["execution_mode"], "host")

    def test_default_cli_returns_a_host_task_instead_of_asking_for_api_key(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"workspace_root": str(self.root), "report_runtime": {"provider": "invalid"}}))
        args = ["local_reports.py", "--config", str(config), "generate", "--run-dir", str(self.run),
                "--taskbook", self.args.taskbook, "--report", self.args.report,
                "--types", "review", "achievement", "--project-name", self.args.project_name]
        output = StringIO()
        with patch.object(sys, "argv", args), redirect_stdout(output):
            self.assertEqual(local_reports.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "needs_model")
        self.assertIn("--request", result["next_command"])


if __name__ == "__main__":
    unittest.main()
