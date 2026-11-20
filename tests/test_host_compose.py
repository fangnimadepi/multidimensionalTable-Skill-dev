"""Focused contract checks against real failure modes, without model/API calls."""

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "project-report"
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "runtime/scripts")]
import host_compose as compose
import host_reports as host
from host_content import brief_locations
from host_fixtures import reply, ACTUAL, NEXT
import test_host_reports as workflow
import test_host_feishu as native
import feishu_batch as batch


class ComposeContractTests(unittest.TestCase):
    def setUp(self):
        self.work = workflow.HostWorkflowTests("test_two_editable_reports_complete_without_any_model_configuration")
        self.work.setUp()
        self.addCleanup(self.work.doCleanups)
        _, self.request = self.work.next()
        self.material = self.work.material()
        self.raw = reply(self.request)["result"]

    def errors(self, value=None):
        return compose.validate(self.raw if value is None else value, self.material, self.work.args.types)

    def test_ordinary_prose_can_mix_current_and_future_without_paragraph_ids(self):
        self.raw["review"]["progress_summary"] = [ACTUAL + NEXT]
        self.raw["achievement"]["short_version"] = [ACTUAL + NEXT]
        self.assertFalse(self.errors())
        self.assertIsInstance(self.raw["review"]["progress_summary"][0], str)

    def test_short_ids_do_not_relax_source_sides_or_goal_role(self):
        plan = self.raw["plans"][0]["sources"][0]
        self.raw["key_facts"][0]["sources"] = [plan]
        self.assertTrue(any("材料侧" in e for e in self.errors()))
        self.raw = reply(self.request)["result"]
        cited = set(self.raw["key_facts"][0]["sources"])
        for key, source in compose.sources(self.material).items():
            if key in cited:
                source["role"] = "target"
        self.assertTrue(any("当前关键成果" in e for e in self.errors()))
        self.assertTrue(any("明确进展判断" in e for e in self.errors()))

    def test_a_finding_cannot_claim_noncompletion_without_report_content(self):
        self.raw["review"]["findings"][0]["sources"] = self.raw["plans"][0]["sources"]
        self.assertTrue(any("不能仅凭任务书" in e for e in self.errors()))

    def test_plan_coverage_and_future_stage_boundaries_remain(self):
        removed = self.raw["plans"].pop()
        self.raw["review"]["comparisons"].pop()
        self.assertTrue(any("计划条件遗漏" in e for e in self.errors()))
        self.raw = reply(self.request)["result"]
        self.raw["review"]["comparisons"][0]["status"] = "后续节点"
        self.assertTrue(any("不能移出" in e for e in self.errors()))
        self.raw["review"]["comparisons"][0]["status"] = "部分完成"
        self.raw["review"]["comparisons"][1]["status"] = "满足"
        normalized = compose.normalize(self.raw)
        self.assertEqual(normalized["review"]["comparisons"][1]["status"], "后续节点")
        self.assertFalse(self.errors(normalized))

    def test_all_metric_variants_and_all_decisions_are_in_the_focused_review(self):
        self.raw["achievement"]["short_version"] = ["已完成8B验证。", "仅完成8B验证，仍需确认适用范围。"]
        groups = compose.focus(self.raw, self.material, self.work.args.types)
        decision = [g for g in groups if g["kind"] == "计划与进展"]
        self.assertEqual(len(decision), len(self.raw["plans"]))
        self.assertEqual(len([g for g in groups if g["kind"] == "项目出入点"]), 1)
        snippets = json.dumps(groups, ensure_ascii=False)
        for value in self.raw["achievement"]["short_version"]:
            self.assertIn(value, snippets)
        accepted = {"reviewed": [g["id"] for g in groups], "issues": [], "editorial_notes": ["文风建议"]}
        self.assertFalse(compose.check(accepted, groups))
        accepted["reviewed"].pop()
        self.assertTrue(compose.check(accepted, groups))

    def test_field_patch_preserves_other_sections_and_rejects_unrelated_or_overlapping_paths(self):
        before = copy.deepcopy(self.raw)
        self.assertEqual(compose.patch_roots(["key_facts/0/text:关键数字缺少原文", "review/progress_summary/0:实际成果数字需补来源"]), ["/key_facts", "/review/progress_summary/0"])
        roots = compose.patch_roots(["review/conclusion/0:需要保留状态边界"])
        fixed = compose.apply_patches(self.raw, [{"path": "/review/conclusion/0", "value": "有验证进展，发布状态仍需确认。"}], roots)
        self.assertEqual(fixed["achievement"], before["achievement"])
        self.assertEqual(self.raw, before)
        for patches in ([{"path": "/achievement/value_title", "value": "无关改写"}],
                        [{"path": "/review/conclusion", "value": []}, {"path": "/review/conclusion/0", "value": "重复"}]):
            with self.assertRaises(ValueError):
                compose.apply_patches(self.raw, patches, roots)
        self.assertEqual(self.raw, before)

    def test_patch_response_contains_only_the_bad_field_not_a_complete_draft(self):
        self.raw["review"]["progress_summary"][0] = "已完成32B模型验证。"
        self.work.respond(self.request, self.raw)
        stage, repair = self.work.next()
        self.assertEqual(stage, "repair")
        self.assertIn("/review/progress_summary/0", repair["allowed_roots"])
        self.assertNotIn("/achievement", repair["allowed_roots"])
        self.assertNotIn("draft", repair)
        self.assertNotIn("result", repair["response_envelope"])
        self.assertTrue(all({"path", "value"} <= set(row) for row in repair["fields"]))

    def test_malformed_model_fields_return_actionable_errors_instead_of_type_errors(self):
        changes = [(["plans", 0, "scope"], []), (["review", "findings", 0, "category"], {}),
                   (["review", "comparisons", 0, "status"], []), (["plans", 0, "sources"], None),
                   (["achievement", "card"], None), (["key_facts"], {})]
        for path, replacement in changes:
            with self.subTest(path=path):
                raw = copy.deepcopy(self.raw)
                target = raw
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                self.assertTrue(self.errors(compose.normalize(raw)))

    def test_theme_appendix_is_small_and_keeps_both_sides_of_the_discrepancy(self):
        self.work.respond(self.request)
        _, request = self.work.next()
        self.work.respond(request)
        receipt = self.work.next()[1]
        for doc in receipt["documents"].values():
            markdown = Path(doc["markdown"]).read_text()
            appendix = markdown.split("## 材料来源与重点定位", 1)[1]
            self.assertLess(len(appendix), 700)
            self.assertNotIn("对应内容", appendix)
            self.assertNotIn("原材料位置", appendix)
            self.assertIn("任务书", appendix)
            self.assertIn("总结报告", appendix)
        self.assertEqual(receipt["host_steps_completed"], 2)
        self.assertEqual(set(receipt["timing"]), {"material_seconds", "core_validation_seconds", "host_wait_seconds", "document_export_seconds"})

    def test_forecast_without_numeric_key_facts_still_has_a_meaningful_review(self):
        from host_fixtures import compose_reply
        raw = compose_reply({**self.request, "requested_types": ["forecast"]})
        raw["key_facts"] = []
        self.assertFalse(compose.validate(raw, self.material, ["forecast"]))
        groups = compose.focus(raw, self.material, ["forecast"])
        self.assertTrue(any(g["kind"] == "目标与交付条件" for g in groups))
        self.assertTrue(all(s.startswith("A") for s in groups[0]["sources"]))
        contexts = host.source_context(self.material)
        self.assertTrue(any("section" in item for item in contexts))

    def test_pdf_locations_compress_page_runs(self):
        text = brief_locations([{"side": "report", "location": f"原 PDF 第{n}页"} for n in (3, 4, 5, 9)])
        self.assertEqual(text, "总结报告：第3—5、9页")

    def test_material_files_are_complete_and_bound_when_a_task_is_split(self):
        # Feed enough immutable units to require separate readable material files.
        material = copy.deepcopy(self.material)
        originals = list(material["sources"])
        material["sources"] = [{**originals[0], "text": "材料正文" * 500, "source_id": f"private-{i}"} for i in range(40)]
        state_path = next((self.work.run / "_host").glob("*/state.json"))
        state = json.loads(state_path.read_text())
        state["pending"] = None
        payload = {"project_name": "大材料", "source_context": host.source_context(material), "required_plan_sources": []}
        host.request(state_path.parent, state, "compose", 0, payload, 5)
        request = json.loads(Path(state["pending"]["request_file"]).read_text())
        files = request["source_files"]
        self.assertGreater(len(files), 1)
        self.assertEqual(sum(Path(p).read_text().count("### A") for p in files), 40)
        self.assertNotIn("材料正文", Path(request["task_file"]).read_text())
        Path(files[-1]).write_text("篡改")
        with self.assertRaisesRegex(ValueError, "原材料分段"):
            host.response(state_path.parent, state, None)


class SnapshotHandoffTests(unittest.TestCase):
    def test_no_remote_record_reads_between_model_steps_but_changed_inputs_stop_archive(self):
        work = native.NativeFeishuTests("test_mixed_batch_archives_both_reports_then_forecast_and_reports_missing_material")
        work.setUp()
        self.addCleanup(work.doCleanups)
        inventory = batch.inspect(work.connector, work.root, native.options(project=["完整项目"]))
        args = native.SimpleNamespace(batch=Path(inventory["batch"]), limit=None, retry_failed=False, response=None)
        with patch.object(batch, "current_record", wraps=batch.current_record) as remote:
            result = batch.run(work.connector, work.file, work.config, work.root, args)
            reads = remote.call_count
            task = result["model_task"]
            request = json.loads(Path(task["request_file"]).read_text())
            Path(task["response_file"]).write_text(json.dumps(reply(request), ensure_ascii=False))
            args.response = Path(task["response_file"])
            result = batch.run(work.connector, work.file, work.config, work.root, args)
            self.assertEqual(remote.call_count, reads)
            # An updated Feishu input may not be silently replaced by the snapshot.
            work.connector.rows[0]["fldReport"][0]["file_token"] = "changed-input"
            task = result["model_task"]
            request = json.loads(Path(task["request_file"]).read_text())
            Path(task["response_file"]).write_text(json.dumps(reply(request), ensure_ascii=False))
            args.response = Path(task["response_file"])
            result = batch.run(work.connector, work.file, work.config, work.root, args)
            self.assertEqual(result["counts"], {"archive_failed": 1})
            self.assertGreater(remote.call_count, reads)
            self.assertFalse(work.connector.rows[0]["fldOutput"])


if __name__ == "__main__":
    unittest.main()
