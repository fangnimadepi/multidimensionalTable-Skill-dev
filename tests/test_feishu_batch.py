"""Verify the batch boundary around attachments, checked output and resume."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-report/scripts"))
import feishu_batch as batch
from feishu_connector import ConnectorError, NeedsSelection


FIELDS = [
    {"id": "fldName", "name": "项目名", "type": "text"},
    {"id": "fldPlan", "name": "项目任务书", "type": "attachment"},
    {"id": "fldPhase", "name": "项目中期报告", "type": "attachment"},
    {"id": "fldArchive", "name": "总结报告", "type": "attachment", "description": "AI生成的项目总结报告存放列"},
]


def attachment(token, name, contents):
    return {"file_token": token, "name": name, "size": len(contents)}


class FakeConnector:
    def __init__(self, directory):
        self.directory = directory
        self.files = {"src1": b"taskbook", "src2": b"summary", "old1": b"old generated", "other": b"manual attachment"}
        self.row = {
            "record_id": "recOne", "fldName": "样例项目",
            "fldPlan": [attachment("src1", "项目.docx", self.files["src1"])],
            "fldPhase": [attachment("src2", "项目.pdf", self.files["src2"])],
            "fldArchive": [attachment("old1", "旧阶段成果报告.docx", self.files["old1"]),
                           attachment("other", "人工说明.docx", self.files["other"])],
        }
        self.events = []
        self.partial = False

    def resolve(self, target, table):
        return {"base_token": "example", "table_id": "tblOne", "table_name": "测试表", "view_id": "vewOne", "record_id": None}

    def fields(self, table):
        return FIELDS

    def records(self, table, fields, view):
        return [self.row, {"record_id": "recEmpty", **dict.fromkeys(fields)}]

    def record(self, table, fields, record_id):
        return self.row

    def download(self, table, record_id, item, path):
        self.events.append(("download", item["file_token"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.files[item["file_token"]])
        return batch.file_hash(path)

    def table_call(self, action, table, *arguments):
        self.assert_write_target(arguments)
        output = self.row["fldArchive"]
        if action == "record-upload-attachment":
            files = [Path(value) for flag, value in zip(arguments, arguments[1:]) if flag == "--file"]
            self.events.append(("upload", len(files)))
            for file in files:
                token = f"new{len(self.files)}"
                content = (self.directory / file).read_bytes()
                self.files[token] = content
                output.append(attachment(token, file.name, content))
                if self.partial:
                    self.partial = False
                    raise ConnectorError("上传中途中断")
        elif action == "record-remove-attachment":
            tokens = {value for flag, value in zip(arguments, arguments[1:]) if flag == "--file-token"}
            self.events.append(("remove", tokens))
            self.row["fldArchive"] = [file for file in output if file["file_token"] not in tokens]
        else:
            raise AssertionError(action)

    @staticmethod
    def assert_write_target(arguments):
        if list(arguments[:4]) != ["--record-id", "recOne", "--field-id", "fldArchive"]:
            raise AssertionError("Write was pointed at an unselected record or a source field")


def inspect_args(**overrides):
    return SimpleNamespace(**{
        "target": "测试表", "table": None, "types": ["review", "achievement"], "archive": True, "replace": True,
        "whole_table": False, "all": False, "count": 1, "first": None, "project": None, "material": None,
        "name_column": None, "taskbook_column": None, "report_column": None, "output_column": None, "no_report": False,
        **overrides,
    })


class FeishuBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.connector = FakeConnector(self.root)
        self.config = self.root / "config.json"

    def make_report(self, args, runtime, run_dir, config, model):
        bundle = run_dir / "reports/attempt-one"
        bundle.mkdir(parents=True)
        documents = {}
        for kind in args.types:
            file = bundle / (kind + ".docx")
            file.write_bytes(kind.encode())
            documents[kind] = {"docx": str(file), "bytes": file.stat().st_size, "sha256": batch.file_hash(file)}
        receipt = {"status": "checked", "record_id": args.record_id, "checks": {
            kind: {"passed": True} for kind in [*args.types, "paired"]}, "seconds": 1,
            "material_hashes": {"plan": batch.file_hash(run_dir / args.taskbook), "report": batch.file_hash(run_dir / args.report)},
            "documents": documents, "receipt": str(bundle / "checked.json")}
        batch.atomic_write_json(bundle / "checked.json", receipt)
        return receipt

    def test_standard_command_plans_both_reports_and_archives_them_to_the_project(self):
        batch.atomic_write_json(self.config, {"workspace_root": str(self.root)})
        command = ["feishu_batch.py", "--config", str(self.config), "deliver", "--target", "测试表",
                   "--project", "样例项目"]
        fields = [{**FIELDS[0], "name": "文本"}, *FIELDS[1:]]
        output = StringIO()
        with (patch.object(sys, "argv", command), patch.object(batch, "Connector", return_value=self.connector),
              patch.object(self.connector, "fields", return_value=fields),
              patch.object(batch, "generate", side_effect=self.make_report),
              patch.object(batch, "setup_runtime", return_value=(self.root, {"api_key_env": "config-key"})),
              patch.object(batch, "api_key", return_value=True),
              redirect_stdout(output)):
            self.assertEqual(batch.main(), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["archive"])
        self.assertEqual(result["requested_types"], ["review", "achievement"])
        self.assertEqual(result["columns"]["name"], "文本")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"], {"archived": 1})
        receipt = batch.load_json(Path(result["projects"][0]["archive"]["receipt"]))
        self.assertEqual(set(receipt["verified"]), {"review", "achievement"})
        self.assertEqual(self.connector.events[:3], [("download", "src1"), ("download", "src2"), ("upload", 2)])
        self.assertCountEqual(self.connector.events[3:],
                              [("download", item["file_token"]) for item in receipt["verified"].values()])

    def test_inspect_with_only_achievement_and_no_delivery_is_rejected_before_feishu_access(self):
        command = ["feishu_batch.py", "--config", str(self.config), "inspect", "--target", "测试表",
                   "--project", "样例项目", "--types", "achievement", "--name-column", "文本"]
        output = StringIO()
        with (patch.object(sys, "argv", command), patch.object(batch, "Connector") as connector,
              redirect_stderr(output), self.assertRaises(SystemExit) as stopped):
            batch.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("--archive --local-only", output.getvalue())
        connector.assert_not_called()

    def test_explicit_single_report_keeps_local_preview(self):
        batch.atomic_write_json(self.config, {"workspace_root": str(self.root)})
        command = ["feishu_batch.py", "--config", str(self.config), "inspect", "--target", "测试表",
                   "--types", "achievement", "--local-only"]
        output = StringIO()
        with (patch.object(sys, "argv", command), patch.object(batch, "Connector", return_value=self.connector),
              redirect_stdout(output)):
            self.assertEqual(batch.main(), 0)
        plan = json.loads(output.getvalue())
        self.assertFalse(plan["archive"])
        self.assertEqual(plan["requested_types"], ["achievement"])
        self.assertNotIn("output", plan["columns"])

    def test_partial_archive_resumes_without_regeneration_or_duplicate_upload(self):
        plan = batch.inspect(self.connector, self.root, inspect_args())
        self.assertEqual(plan["total"], 1)
        self.assertEqual(plan["columns"]["output"], "总结报告")
        older = self.root / "example_tblOne/recOne/previous/archive.json"
        old = {**self.connector.row["fldArchive"][0], "sha256": hashlib.sha256(self.connector.files["old1"]).hexdigest()}
        batch.atomic_write_json(older, {"status": "archived", "record_id": "recOne", "field_id": "fldArchive",
                                       "verified": {"achievement": old}})
        self.connector.partial = True
        args = SimpleNamespace(batch=Path(plan["batch"]), limit=None, retry_failed=False)
        with (patch.object(batch, "generate", side_effect=self.make_report) as generate,
              patch.object(batch, "setup_runtime", return_value=(self.root, {"api_key_env": "config-key"})),
              patch.object(batch, "api_key", return_value=True)):
            first = batch.run(self.connector, self.config, {}, self.root, args)
            self.assertEqual(first["counts"], {"archive_failed": 1})
            self.assertIn("old1", {file["file_token"] for file in self.connector.row["fldArchive"]})
            second = batch.run(self.connector, self.config, {}, self.root, args)
            self.assertEqual(second["counts"], {"archived": 1})
            generate.assert_called_once()
        self.assertEqual(self.connector.events[:4], [("download", "src1"), ("download", "src2"), ("upload", 2), ("upload", 1)])
        self.assertEqual(self.connector.events[-1], ("remove", {"old1"}))
        self.assertEqual(len(self.connector.row["fldArchive"]), 3)
        self.assertIn("other", {file["file_token"] for file in self.connector.row["fldArchive"]})

    def test_scope_mismatch_and_using_input_as_output_stop_at_inspect(self):
        for arguments in (inspect_args(count=2), inspect_args(output_column="项目中期报告")):
            with self.assertRaises(NeedsSelection):
                batch.inspect(self.connector, self.root, arguments)
        self.assertEqual(self.connector.events, [])

    def test_project_short_name_requires_a_unique_delimited_match(self):
        rows = [{"record_id": "recTwo", "project_name": "test02:跨顶层设计dtco"},
                {"record_id": "recThree", "project_name": "test03：通专模型数智大脑"}]
        args = inspect_args(project=["test02", "test03"])
        self.assertEqual(batch.choose_projects(rows, args, None), rows)
        rows.append({"record_id": "recOther", "project_name": "test03:另一项目"})
        with self.assertRaises(NeedsSelection):
            batch.choose_projects(rows, args, None)
        with self.assertRaises(NeedsSelection):
            batch.choose_projects(rows, inspect_args(project=["test0"]), None)

    def test_missing_archive_column_does_not_plan_a_local_only_delivery(self):
        with patch.object(self.connector, "fields", return_value=FIELDS[:3]):
            with self.assertRaisesRegex(NeedsSelection, "未找到明确的归档附件列"):
                batch.inspect(self.connector, self.root, inspect_args(replace=False))
        self.assertEqual(self.connector.events, [])

    def test_changed_input_stops_download_and_generation_when_resuming_plan(self):
        plan = batch.inspect(self.connector, self.root, inspect_args())
        self.connector.row["fldPhase"] = []
        args = SimpleNamespace(batch=Path(plan["batch"]), limit=None, retry_failed=False)
        with patch.object(batch, "generate") as generate:
            result = batch.run(self.connector, self.config, {}, self.root, args)
            self.assertEqual(result["counts"], {"failed": 1})
            generate.assert_not_called()
        self.assertEqual(self.connector.events, [])

    def test_validation_failure_stops_repeated_generation_until_runtime_changes(self):
        plan = batch.inspect(self.connector, self.root, inspect_args())
        args = SimpleNamespace(batch=Path(plan["batch"]), limit=None, retry_failed=True)
        model = {"api_key_env": "config-key"}
        failure = batch.WorkerFailure("正文检查未通过", self.root / "attempt",
                                      error_code="report_validation_failed")
        with (patch.object(batch, "generate", side_effect=failure) as generate,
              patch.object(batch, "setup_runtime", return_value=(self.root, model)),
              patch.object(batch, "api_key", return_value=True)):
            result = batch.run(self.connector, self.config, {}, self.root, args)
            self.assertFalse(result["projects"][0]["failure"]["retryable"])
            result = batch.run(self.connector, self.config, {}, self.root, args)
            self.assertEqual(result["counts"], {"failed": 1})
            generate.assert_called_once()
        with (patch.object(batch, "generate", side_effect=self.make_report) as generate,
              patch.object(batch, "setup_runtime", return_value=(self.root / "updated-engine", model)),
              patch.object(batch, "api_key", return_value=True)):
            result = batch.run(self.connector, self.config, {}, self.root, args)
            self.assertEqual(result["counts"], {"archived": 1})
            generate.assert_called_once()
            self.assertNotIn("failure", result["projects"][0])


if __name__ == "__main__":
    unittest.main()
