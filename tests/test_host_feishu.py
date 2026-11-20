"""The native workflow must finish both files and archive, including mixed inputs."""

from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "project-report"
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "runtime/scripts")]
import feishu_batch as batch
import bootstrap
from host_fixtures import documents, reply


class NativeConnector:
    def __init__(self, directory):
        self.directory = directory
        plan, report = documents()
        self.files = {"plan": plan, "report": report}
        self.events = []
        self.rows = [
            {"record_id": "recOne", "fldName": "完整项目", "fldPlan": [self.file("plan", "任务书.docx")],
             "fldReport": [self.file("report", "中期总结.docx")], "fldOutput": []},
            {"record_id": "recTwo", "fldName": "仅任务书项目", "fldPlan": [self.file("plan", "任务书.docx")],
             "fldReport": [], "fldOutput": []},
            {"record_id": "recMissing", "fldName": "缺任务书项目", "fldPlan": [],
             "fldReport": [self.file("report", "中期总结.docx")], "fldOutput": []},
        ]

    def file(self, token, name):
        return {"file_token": token, "name": name, "size": len(self.files[token])}

    def resolve(self, target, table):
        return {"base_token": "example", "table_id": "tblNative", "table_name": "合成测试表",
                "view_id": "vewNative", "record_id": None}

    def fields(self, table):
        return [{"id": key, "name": name, "type": kind} for key, name, kind in (
            ("fldName", "项目名", "text"), ("fldPlan", "项目任务书", "attachment"),
            ("fldReport", "项目中期报告", "attachment"), ("fldOutput", "项目报告归档", "attachment"))]

    def records(self, table, fields, view):
        return self.rows

    def record(self, table, fields, record_id):
        return next(row for row in self.rows if row["record_id"] == record_id)

    def download(self, table, record_id, file, path):
        row = self.record(table, [], record_id)
        tokens = {f["file_token"] for field in ("fldPlan", "fldReport", "fldOutput") for f in row[field]}
        if file["file_token"] not in tokens:
            raise AssertionError("Cross-project attachment access")
        self.events.append(("download", record_id, file["file_token"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.files[file["file_token"]])
        return batch.file_hash(path)

    def table_call(self, action, table, *arguments):
        if action != "record-upload-attachment" or arguments[2:4] != ("--field-id", "fldOutput"):
            raise AssertionError("Unexpected write operation")
        row = self.record(table, [], arguments[1])
        self.events.append(("upload", row["record_id"]))
        for flag, relative in zip(arguments, arguments[1:]):
            if flag == "--file":
                file = self.directory / relative
                token = "out" + str(len(self.files))
                self.files[token] = file.read_bytes()
                row["fldOutput"].append(self.file(token, file.name))


def options(**changes):
    return SimpleNamespace(**{"target": "测试表", "table": None, "types": ["review", "achievement"], "mode": "host",
                             "archive": True, "replace": False, "whole_table": False, "all": True,
                             "count": None, "first": None, "project": None, "material": None,
                             "name_column": None, "taskbook_column": None, "report_column": None,
                             "output_column": None, "no_report": False, **changes})


class NativeFeishuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.connector = NativeConnector(self.root)
        self.file = self.root / "config.json"
        self.config = {"workspace_root": str(self.root), "report_runtime": {"provider": "not-configured"}}
        self.file.write_text(json.dumps(self.config))

    def test_mixed_batch_archives_both_reports_then_forecast_and_reports_missing_material(self):
        inventory = batch.inspect(self.connector, self.root, options())
        args = SimpleNamespace(batch=Path(inventory["batch"]), limit=None, retry_failed=False, response=None)
        with ExitStack() as guards:
            guards.enter_context(patch("llm_client.complete_json", side_effect=AssertionError("Unexpected API call")))
            guards.enter_context(patch.object(batch, "setup_runtime", side_effect=AssertionError("Read API settings")))
            guards.enter_context(patch.object(batch, "api_key", side_effect=AssertionError("Read API key")))
            for _ in range(30):
                result = batch.run(self.connector, self.file, self.config, self.root, args)
                if result["status"] != "needs_model":
                    break
                task = result["model_task"]
                self.assertIn("next_command", result)
                request = json.loads(Path(task["request_file"]).read_text())
                Path(task["response_file"]).write_text(json.dumps(reply(request), ensure_ascii=False))
                args.response = Path(task["response_file"])
            else:
                self.fail("Batch did not terminate")
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["counts"], {"archived": 2, "blocked": 1})
        self.assertEqual([len(row["fldOutput"]) for row in self.connector.rows], [2, 1, 0])
        self.assertEqual([event for event in self.connector.events if event[0] == "upload"],
                         [("upload", "recOne"), ("upload", "recTwo")])
        self.assertEqual(result["projects"][1]["types"], ["forecast"])
        self.assertIn("缺少", result["projects"][2]["reason"])
        uploads = list(self.connector.events)
        replayed = batch.run(self.connector, self.file, self.config, self.root, args)
        self.assertEqual(replayed["counts"], result["counts"])
        self.assertEqual(self.connector.events, uploads)
        # Neither initialization nor report execution changes the API config.
        self.assertEqual(json.loads(self.file.read_text()), self.config)

    def test_explicit_api_mode_still_routes_to_the_existing_engine(self):
        inventory = batch.inspect(self.connector, self.root, options(mode="api", project=["完整项目"]))
        args = SimpleNamespace(batch=Path(inventory["batch"]), limit=1, retry_failed=False, response=None)
        failure = batch.WorkerFailure("API engine sentinel", self.root, error_code="report_worker_failed")
        with (patch.object(batch, "setup_runtime", return_value=(ROOT / "runtime", {"api_key_env": "synthetic"})) as api,
              patch.object(batch, "setup_host_runtime") as native, patch.object(batch, "api_key", return_value=True),
              patch.object(batch, "generate", side_effect=failure) as generate):
            result = batch.run(self.connector, self.file, self.config, self.root, args)
        api.assert_called_once()
        native.assert_not_called()
        generate.assert_called_once()
        self.assertEqual(result["counts"], {"failed": 1})
        self.assertFalse(self.connector.rows[0]["fldOutput"])

    def test_default_doctor_does_not_open_either_kind_of_model_config(self):
        manual = self.root / "model.local.json"
        manual.write_text("malformed optional API config")
        with (patch.object(bootstrap, "LOCAL_MODEL_CONFIG", manual),
              patch.dict("os.environ", {"JINGYAN_CONFIG": str(self.file)}),
              patch("workbuddy_models.read_models", side_effect=AssertionError("Read WorkBuddy secrets")),
              patch("model_setup.manual_model_options", side_effect=AssertionError("Read optional API secrets"))):
            state = bootstrap.configuration_state(self.root)
        self.assertEqual(state["execution_mode"], "host")
        self.assertFalse(state["model_setup_needed"])
        self.assertTrue(state["optional_api_config_present"])
        self.assertEqual(manual.read_text(), "malformed optional API config")

    def test_failed_project_response_does_not_poison_the_next_project(self):
        inventory = batch.inspect(self.connector, self.root, options())
        args = SimpleNamespace(batch=Path(inventory["batch"]), limit=None, retry_failed=False, response=None)
        for _ in range(30):
            result = batch.run(self.connector, self.file, self.config, self.root, args)
            if result["status"] != "needs_model":
                break
            pending = result["model_task"]
            request = json.loads(Path(pending["request_file"]).read_text())
            answer = reply(request)
            if request["project_name"] == "完整项目":
                if request["stage"] == "compose":
                    answer["result"]["plans"][0]["sources"] = ["A9999"]
                elif request["stage"] == "repair":
                    answer["patches"] = [{"path":"/plans/0/sources","value":["A9999"]}]
            Path(pending["response_file"]).write_text(json.dumps(answer, ensure_ascii=False))
            args.response = Path(pending["response_file"])
        else:
            self.fail("Batch did not finish after one project's bounded failure")
        self.assertEqual(result["counts"], {"failed": 1, "archived": 1, "blocked": 1})
        self.assertEqual([len(row["fldOutput"]) for row in self.connector.rows], [0, 1, 0])
        self.assertEqual(result["projects"][0]["failure"]["error_code"], "host_content_check_failed")


if __name__ == "__main__":
    unittest.main()
