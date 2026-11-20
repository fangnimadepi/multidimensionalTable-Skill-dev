"""Regression coverage for real host failures: quoting, schemas and self-debugging."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "project-report"
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "runtime/scripts")]
import host_reports
import host_task
from host_protocol import HostTaskPending, HostReportFailure
from host_content import validate_facts, validate_draft, normalize_draft, render
from host_materials import catalog, compact_source, batches
from host_tasks import command, repair_details
from host_fixtures import documents, reply
from test_host_feishu import NativeConnector, options
import feishu_batch


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / "project"
        self.config_file = self.root / "config.json"
        self.config = {"workspace_root": str(self.root)}
        self.config_file.write_text(json.dumps(self.config))
        for role, data in zip(("taskbook", "report"), documents()):
            p = self.run / "raw" / role / (role + ".docx")
            p.parent.mkdir(parents=True)
            p.write_bytes(data)
        context = {"kind": "local", "config_file": str(self.config_file), "run_dir": str(self.run),
                   "taskbook": "raw/taskbook/taskbook.docx", "report": "raw/report/report.docx",
                   "types": ["review", "achievement"], "record_id": "recOne", "project_name": "示例项目"}
        self.args = SimpleNamespace(**{k: context[k] for k in ("taskbook", "report", "types", "record_id", "project_name")},
                                    response=None, continuation=context)

    def pending(self):
        with self.assertRaises(HostTaskPending) as stopped:
            host_reports.advance(self.args, ROOT / "runtime", self.run, self.config)
        return stopped.exception.task

    def answer(self, task):
        request = json.loads(Path(task["request_file"]).read_text())
        answer = reply(request)
        if request["stage"] == "facts":
            for row in answer["result"]["plan_items"] + answer["result"]["facts"]:
                row["citations"] = [c["source_id"] for c in row["citations"]]
        Path(task["response_file"]).write_text(json.dumps(answer, ensure_ascii=False))
        return request

    def test_submit_entry_completes_source_ids_to_two_docs_without_api_or_module_import_by_host(self):
        task = self.pending()
        stages = []
        with (patch("llm_client.complete_json", side_effect=AssertionError("Unexpected API call")),
              patch("local_reports.setup_runtime", side_effect=AssertionError("Unexpected model config"))):
            for _ in range(10):
                request = self.answer(task)
                stages.append(request["stage"])
                result = host_task.submit(task["request_file"], self.config_file, self.config)
                if result["status"] == "checked":
                    break
                self.assertEqual(result["status"], "needs_model")
                task = result["model_task"]
            else:
                self.fail("New submit workflow did not terminate")
        self.assertEqual(stages, ["facts", "draft", "check"])
        self.assertEqual(set(result["documents"]), {"review", "achievement"})
        state = json.loads((Path(result["diagnostics"]) / "state.json").read_text())
        self.assertTrue(state["facts"][0]["plan_items"][0]["citations"][0]["quote"])

    def test_submit_command_runs_in_a_fresh_python_process_without_pythonpath(self):
        task = self.pending()
        self.answer(task)
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/host_task.py"), "--config", str(self.config_file),
                                 "submit", "--request", task["request_file"]], cwd=self.root,
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["model_task"]["stage"], "draft")

    def test_task_includes_real_skeleton_exact_command_and_no_default_approved_checks(self):
        task = self.pending()
        request = json.loads(Path(task["request_file"]).read_text())
        template = json.loads(Path(task["response_template"]).read_text())
        self.assertEqual(template["request_id"], request["request_id"])
        self.assertEqual(template["result"]["plan_items"], [])
        self.assertEqual(template["result"]["coverage"][0]["disposition"], "")
        self.assertIn(str(Path(sys.executable)), task["submit_command"])
        self.assertIn("host_task.py", task["submit_command"])
        text = Path(task["task_file"]).read_text()
        self.assertIn("submit", text)
        self.assertIn(request["sources"][0]["source_id"], text)
        self.assertNotIn("next_command", request["instructions"])

    def test_readable_task_includes_fact_schema_and_untruncated_draft_identifiers(self):
        task = self.pending()
        text = Path(task["task_file"]).read_text()
        schema = text.split("## 响应字段示例", 1)[1].split("```json", 1)[1].split("```", 1)[0]
        example = json.loads(schema)
        self.assertIn("requirement", example["result"]["plan_items"][0])
        self.assertIn("kind", example["result"]["facts"][0])
        self.answer(task)
        result = host_task.submit(task["request_file"], self.config_file, self.config)
        draft_task = Path(result["model_task"]["task_file"]).read_text()
        request = json.loads(Path(result["model_task"]["request_file"]).read_text())
        # Reproduce a host reader that caps each line, rather than merely
        # checking that the program wrote the identifiers somewhere on disk.
        visible = "\n".join(line[:2000] for line in draft_task.splitlines())
        for fact in request["facts"]["facts"]:
            self.assertIn(fact["fact_id"], visible)
            self.assertIn(fact["text"], visible)

    def test_missing_response_and_repeated_polling_stop_instead_of_an_endless_wait(self):
        task = self.pending()
        with self.assertRaisesRegex(ValueError, "尚未创建"):
            host_task.submit(task["request_file"], self.config_file, self.config)
        self.pending()
        self.pending()
        with self.assertRaises(HostReportFailure) as stopped:
            host_reports.advance(self.args, ROOT / "runtime", self.run, self.config)
        self.assertEqual(stopped.exception.stage, "no_progress")

    def test_processed_task_cannot_be_replayed_to_consume_a_different_pending_response(self):
        task = self.pending()
        self.answer(task)
        next_task = host_task.submit(task["request_file"], self.config_file, self.config)
        self.assertEqual(next_task["model_task"]["stage"], "draft")
        with self.assertRaisesRegex(ValueError, "已经提交"):
            host_task.submit(task["request_file"], self.config_file, self.config)

    def test_changed_reading_task_and_cross_workspace_config_are_rejected(self):
        task = self.pending()
        self.answer(task)
        Path(task["task_file"]).write_text("changed instructions")
        with self.assertRaisesRegex(ValueError, "任务说明"):
            host_task.submit(task["request_file"], self.config_file, self.config)
        wrong_config = self.root / "other.json"
        wrong_config.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, "个人工作区"):
            host_task.submit(task["request_file"], wrong_config, self.config)

    def test_comparison_units_normalize_without_discarding_conflicting_sources(self):
        task = self.pending()
        self.answer(task)
        result = host_task.submit(task["request_file"], self.config_file, self.config)
        request = json.loads(Path(result["model_task"]["request_file"]).read_text())
        draft = reply(request)["result"]
        row = draft["review"]["comparisons"][0]
        for field in ("progress", "judgement"):
            row[field] = {"text": row[field], "kind": "事实" if field == "progress" else "分析",
                          "plan_ids": [] if field == "progress" else [row["item_id"]], "fact_ids": row["fact_ids"]}
        normalized, errors = normalize_draft(draft)
        self.assertFalse(errors)
        material = json.loads((Path(task["request_file"]).parent / "materials.json").read_text())
        self.assertFalse(validate_draft(normalized, request["facts"], material, self.args.types)[1])
        row["progress"]["plan_ids"] = [row["item_id"]]
        self.assertTrue(normalize_draft(draft)[1])

    def test_source_id_quotes_are_restored_and_missing_101_is_named(self):
        sources = [{"source_id": "S-test", "side": "report", "text": "已发表论文3篇，申请专利1项。",
                    "role": "actual", "plan_candidate": False}]
        raw = {"plan_items": [], "coverage": [], "facts": [
            {"text": "已发表论文3篇，申请专利1项。", "kind": "阶段进展", "citations": ["S-test"]}]}
        checked, errors = validate_facts(raw, sources)
        self.assertFalse(errors)
        self.assertEqual(checked["facts"][0]["citations"][0]["quote"], sources[0]["text"])
        raw["facts"][0]["text"] += "包含Imaging-101成果。"
        self.assertTrue(any("101" in e for e in validate_facts(raw, sources)[1]))
        raw["facts"][0]["citations"] = ["S-other"]
        self.assertTrue(any("未知来源编号 S-other" in e for e in validate_facts(raw, sources)[1]))

    def draft_context(self):
        task = self.pending()
        self.answer(task)
        result = host_task.submit(task["request_file"], self.config_file, self.config)
        request = json.loads(Path(result["model_task"]["request_file"]).read_text())
        material = json.loads((Path(task["request_file"]).parent / "materials.json").read_text())
        return request, material, reply(request)["result"]

    def test_report_background_date_may_qualify_current_results_but_never_replace_them(self):
        request, material, draft = self.draft_context()
        date_source = {"source_id": "S-date", "side": "report", "role": "body", "text": "项目于2025年12月启动。", "location": "原 PDF 第3页"}
        material["sources"].append(date_source)
        background = {"fact_id": "F-date", "kind": "背景说明", "text": date_source["text"], "citations": ["S-date"]}
        request["facts"]["facts"].append(background)
        actual = draft["review"]["progress_summary"][0]
        actual["text"] = date_source["text"] + actual["text"]
        actual["fact_ids"].append("F-date")
        self.assertFalse(validate_draft(draft, request["facts"], material, self.args.types)[1])
        date_source["side"] = "plan"
        self.assertTrue(any("实际完成" in e for e in validate_draft(draft, request["facts"], material, self.args.types)[1]))
        date_source["side"] = "report"
        date_source["role"] = "proof"
        self.assertTrue(any("实际完成" in e for e in validate_draft(draft, request["facts"], material, self.args.types)[1]))
        date_source["role"] = "body"
        actual["fact_ids"] = ["F-date"]
        self.assertTrue(any("实际完成" in e for e in validate_draft(draft, request["facts"], material, self.args.types)[1]))

    def test_overall_progress_is_preserved_as_reference_without_a_current_stage_verdict(self):
        request, material, draft = self.draft_context()
        current = request["facts"]["plan_items"][0]
        current["scope"] = "总体"
        normalized, errors = normalize_draft(draft, request["facts"])
        self.assertFalse(errors)
        row = normalized["review"]["comparisons"][0]
        self.assertEqual(row["status"], "总体参考")
        self.assertEqual(row["progress_status"], "部分完成")
        self.assertEqual(row["progress"], draft["review"]["comparisons"][0]["progress"])
        self.assertEqual(normalized["review"]["findings"][0]["scope_note"], "总体目标进展参考")
        self.assertFalse(validate_draft(normalized, request["facts"], material, self.args.types)[1])
        # A current condition must never disappear into an automatically filled
        # future/general reference row.
        current["scope"] = "本期"
        normalized, _ = normalize_draft(normalized, request["facts"])
        self.assertTrue(any("移出对照" in e for e in validate_draft(normalized, request["facts"], material, self.args.types)[1]))

    def test_follow_up_statement_is_not_forced_to_pretend_it_is_a_current_result(self):
        request, material, draft = self.draft_context()
        future = draft["review"]["comparisons"][1]
        next_fact = next(f for f in request["facts"]["facts"] if f["kind"] == "后续计划")
        future.update(progress_kind="材料表述", fact_ids=[next_fact["fact_id"]], progress=next_fact["text"])
        self.assertFalse(validate_draft(draft, request["facts"], material, self.args.types)[1])
        future["progress_kind"] = "事实"
        errors = validate_draft(draft, request["facts"], material, self.args.types)[1]
        self.assertTrue(any("实际完成" in e for e in errors))
        details = repair_details(errors, draft, request["facts"])
        item = next(d for d in details if d["path"] == "review/comparisons/1/progress")
        self.assertEqual(item["index_base"], 0)
        self.assertEqual(item["item_id"], future["item_id"])
        self.assertEqual(item["title"], request["facts"]["plan_items"][1]["title"])
        self.assertEqual(item["selected_facts"][0]["kind"], "后续计划")
        future["progress_kind"], future["status"] = "材料表述", "满足"
        self.assertTrue(any("不能作为明确完成状态" in e for e in validate_draft(draft, request["facts"], material, self.args.types)[1]))
        normalized, _ = normalize_draft(draft, request["facts"])
        self.assertTrue(any("不能作为明确完成状态" in e for e in validate_draft(normalized, request["facts"], material, self.args.types)[1]))

    def test_report_wording_difference_uses_its_actual_source_without_unrelated_achievements(self):
        request, material, draft = self.draft_context()
        next_fact = next(f for f in request["facts"]["facts"] if f["kind"] == "后续计划")
        finding = draft["review"]["findings"][0]
        finding["category"] = "进度与计划节点差异"
        finding["progress"] = {"text": next_fact["text"], "kind": "材料表述", "plan_ids": [],
                               "fact_ids": [next_fact["fact_id"]]}
        claims, errors = validate_draft(draft, request["facts"], material, self.args.types)
        self.assertFalse(errors)
        text, _ = render("review", draft, request["facts"], material, "示例项目",
                         {"plan": Path("任务书.docx"), "report": Path("阶段报告.docx")}, claims)
        self.assertIn("**报告所述：** " + next_fact["text"], text)
        self.assertNotIn("**报告完成情况：** " + next_fact["text"], text)
        # Material statements cannot be relabelled as achieved progress or used
        # to replace the current-result summary with a future target.
        finding["progress"]["kind"] = "事实"
        self.assertTrue(any("实际完成" in e for e in validate_draft(draft, request["facts"], material, self.args.types)[1]))
        finding["progress"]["kind"] = "材料表述"
        draft["review"]["progress_summary"] = [finding["progress"]]
        self.assertTrue(any("实际成果" in e for e in validate_draft(draft, request["facts"], material, self.args.types)[1]))

    def test_material_statement_still_requires_report_source_and_exact_numbers(self):
        request, material, draft = self.draft_context()
        next_fact = next(f for f in request["facts"]["facts"] if f["kind"] == "后续计划")
        progress = draft["review"]["findings"][0]["progress"]
        progress.update(text=next_fact["text"] + "包含64项测试。", kind="材料表述", plan_ids=[], fact_ids=[next_fact["fact_id"]])
        self.assertTrue(any("64" in e for e in validate_draft(draft, request["facts"], material, self.args.types)[1]))
        progress["text"] = next_fact["text"]
        selected = {c if isinstance(c, str) else c["source_id"] for c in next_fact["citations"]}
        for s in material["sources"]:
            if s["source_id"] in selected:
                s["side"] = "plan"
        self.assertTrue(any("材料表述只可引用报告" in e for e in validate_draft(draft, request["facts"], material, self.args.types)[1]))

    def test_malformed_response_still_produces_actionable_feedback(self):
        request, material, draft = self.draft_context()
        draft["review"]["comparisons"][0]["item_id"] = {"unexpected": "object"}
        _, errors = validate_draft(draft, request["facts"], material, self.args.types)
        details = repair_details(errors, draft, request["facts"])
        self.assertTrue(details)
        self.assertTrue(any("计划编号无效" in item["message"] for item in details))

    def test_windows_command_quotes_spaces_unicode_and_apostrophes(self):
        self.assertEqual(command(["C:\\Program Files\\python.exe", "某人's 文件.json"], windows=True),
                         "& 'C:\\Program Files\\python.exe' '某人''s 文件.json'")

    def test_feishu_submit_preserves_source_files_and_archives_mixed_projects(self):
        connector = NativeConnector(self.root)
        inventory = feishu_batch.inspect(connector, self.root, options())
        arguments = SimpleNamespace(batch=Path(inventory["batch"]), limit=None, retry_failed=False, response=None)
        result = feishu_batch.run(connector, self.config_file, self.config, self.root, arguments)
        with patch.object(feishu_batch, "Connector", return_value=connector):
            for _ in range(20):
                if result["status"] != "needs_model":
                    break
                task = result["model_task"]
                self.answer(task)
                result = host_task.submit(task["request_file"], self.config_file, self.config)
            else:
                self.fail("Feishu submit workflow did not terminate")
        self.assertEqual(result["counts"], {"archived": 2, "blocked": 1})
        self.assertEqual([len(row["fldOutput"]) for row in connector.rows], [2, 1, 0])
        self.assertEqual([row["fldPlan"][0]["file_token"] for row in connector.rows[:2]], ["plan", "plan"])


class MaterialPreparationTests(unittest.TestCase):
    def test_known_admin_tables_are_removed_but_unlabelled_research_and_metric_tables_remain(self):
        def paragraph(value, index):
            return {"kind": "paragraph", "text": value, "location": "P" + str(index)}

        def table(value, index):
            return {"kind": "table", "location": "T" + str(index), "rows": [[
                {"location": "T" + str(index) + "-R1-C1", "blocks": [paragraph(value, 1)]}]]}

        document = {"blocks": [paragraph("单位基本情况表", 1), table("统一社会信用代码：123456789", 1),
                               paragraph("项目目标、研究内容和关键科学问题", 2), table("研究目标：构建模型与数据闭环。", 2),
                               paragraph("项目的成果形式、考核指标和评测方式", 3), table("考核要求：2026年完成32B验证。", 3),
                               paragraph("经费预算", 4), table("经费支出明细：设备10万元。", 4)]}
        material = catalog({"plan": document}, {})
        selected = "\n".join(s["text"] for s in material["sources"])
        self.assertNotIn("123456789", selected)
        self.assertNotIn("设备10万元", selected)
        self.assertIn("构建模型与数据闭环", selected)
        self.assertIn("32B", selected)
        self.assertTrue(material["excluded_sections"])
        compact = [compact_source(s) for s in material["sources"]]
        self.assertFalse(any("heading" in s or "location" in s for s in compact))
        self.assertEqual([s for part in batches(material["sources"]) for s in part], material["sources"])


if __name__ == "__main__":
    unittest.main()
