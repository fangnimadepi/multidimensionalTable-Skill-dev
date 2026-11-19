"""Custom models are selected once and keys stay in the host's own config."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "project-report"
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "runtime/scripts")]
import model_setup
import workbuddy_models as host
from local_reports import setup_runtime
from private_model_settings import PROVIDERS


def profile(key="synthetic-key-one", model="qwen3.8-flash", display=None):
    return {"id": model, "name": display or model, "vendor": "Qwen", "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "apiKey": key, "supportsToolCall": True, "useCustomProtocol": False}


class WorkBuddyModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "models.json"
        self.write([profile()])

    def write(self, rows):
        self.path.write_text(json.dumps(rows), encoding="utf-8-sig")

    def test_model_keys_are_read_fresh_without_being_returned_or_copied(self):
        runtime = {"workbuddy_config": str(self.path), "workbuddy_model_id": "qwen3.8-flash"}
        original = self.path.read_bytes()
        with patch.dict(os.environ):
            options = host.model_options(runtime, PROVIDERS)
            self.assertEqual(os.environ[host.KEY_ENV], "synthetic-key-one")
            self.assertEqual(options["provider"], "dashscope")
            self.assertEqual(options["text_model"], "qwen3.8-flash")
            self.assertFalse(options["enable_thinking"])
            self.assertNotIn("synthetic-key", json.dumps(options))
            self.assertEqual(self.path.read_bytes(), original)
            self.write([profile(key="synthetic-key-two")])
            host.model_options(runtime, PROVIDERS)
            self.assertEqual(os.environ[host.KEY_ENV], "synthetic-key-two")

    def test_multiple_models_require_selection_and_return_only_names_and_ids(self):
        self.write({"models": [profile(), profile("another-secret", "qwen3.7-plus", "均衡模型")]})
        with self.assertRaises(host.WorkBuddyModelError) as caught:
            host.choose(self.path)
        details = caught.exception.details()
        self.assertEqual(details["error_code"], "workbuddy_model_selection_required")
        self.assertEqual(len(details["candidates"]), 2)
        self.assertNotIn("secret", json.dumps(details))
        self.assertNotIn("url", json.dumps(details))
        self.assertEqual(host.choose(self.path, "均衡模型")[1], "qwen3.7-plus")
        self.assertEqual(host.choose(self.path, "均衡模型")[2], "qwen3.7-plus")

    def test_host_environment_references_resolve_without_becoming_literal_credentials(self):
        self.write([{**profile(), "apiKey": "${JINGYAN_SYNTHETIC_HOST_KEY}"}])
        with patch.dict(os.environ, {"JINGYAN_SYNTHETIC_HOST_KEY": "example-from-environment"}):
            options = host.model_options({"workbuddy_config": str(self.path)}, PROVIDERS)
            self.assertEqual(os.environ[host.KEY_ENV], "example-from-environment")
            self.assertNotIn("example-from-environment", json.dumps(options))
        with self.assertRaises(host.WorkBuddyModelError):
            host.choose(self.path, "qwen3.8-flash")

    def test_unsupported_or_secret_bearing_endpoints_are_rejected_without_echoing_them(self):
        for changes in ({"url": "https://example.invalid/v1?key=private-value"},
                        {"url": "https://user:private-value@example.invalid/v1"},
                        {"url": "https://example.invalid/messages", "useCustomProtocol": True}):
            self.write([{**profile(), **changes}])
            with self.subTest(changes=changes), self.assertRaises(host.WorkBuddyModelError) as caught:
                host.choose(self.path, "qwen3.8-flash")
            self.assertNotIn("private-value", str(caught.exception))
        self.write([{"id": "builtin", "name": "built-in", "url": "", "apiKey": ""}])
        with self.assertRaises(host.WorkBuddyModelError) as caught:
            host.choose(self.path)
        self.assertEqual(caught.exception.code, "workbuddy_model_unavailable")

    def test_new_users_reuse_one_host_model_but_existing_manual_config_stays_in_control(self):
        config_file = self.root / "鲸研配置.json"
        config = {"report_runtime": {"model_source": "auto", "credentials_file": "absent.env"}}
        manual = self.root / "model.local.json"
        with (patch("runtime_support.prepare_runtime", return_value=ROOT / "runtime"),
              patch("model_setup.LOCAL_MODEL_CONFIG", manual),
              patch.object(host, "config_paths", return_value=[self.path]), patch.dict(os.environ)):
            _, options = setup_runtime(config_file, config)
            self.assertEqual(options["model_source"], "workbuddy")
            manual.write_text(json.dumps({"api_base": "https://example.invalid", "model": "manual-model", "api_key": "manual-key"}))
            _, options = setup_runtime(config_file, config)
            self.assertEqual(options["model_source"], "manual")
        self.assertFalse((self.root / "absent.env").exists())

    def test_binding_saves_only_references_and_failed_verification_keeps_prior_config(self):
        file = self.root / "config.json"
        config = {"report_runtime": {"provider": "deepseek", "model": "deepseek-flash", "credentials_file": "old.env"}}
        file.write_text(json.dumps(config))
        with (patch("runtime_support.prepare_runtime", return_value=ROOT / "runtime"),
              patch("model_setup.verify_connection") as verify, patch.dict(os.environ)):
            result = model_setup.bind_workbuddy(file, config, "qwen3.8-flash", self.path)
            self.assertTrue(result["connection_verified"])
            verify.assert_called_once()
            self.assertEqual(list(file.parent.glob("*.env")), [])
            saved = file.read_bytes()
            self.assertNotIn(b"synthetic-key", saved)
            self.assertEqual(json.loads(saved)["report_runtime"]["workbuddy_model_id"], "qwen3.8-flash")
            verify.side_effect = model_setup.ModelSetupError("balance", "model_balance", status=402)
            with self.assertRaises(model_setup.ModelSetupError):
                model_setup.bind_workbuddy(file, json.loads(saved), "qwen3.8-flash", self.path)
            self.assertEqual(file.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
