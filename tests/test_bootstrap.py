"""Check bounded environment repair without touching a user's real installation."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "project-report/scripts"))
import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_doctor_is_read_only_on_an_uninitialized_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "not-created"
            with (patch.object(bootstrap, "probe_python", return_value=False),
                  patch.object(bootstrap, "configuration_state", return_value={"model_setup_needed": True}),
                  patch.object(bootstrap.venv, "EnvBuilder") as create,
                  patch.object(bootstrap.subprocess, "run") as process):
                result = bootstrap.diagnose(data)
            self.assertTrue(result["read_only"])
            self.assertEqual(result["status"], "initialization_required")
            self.assertFalse(data.exists())
            create.assert_not_called()
            process.assert_not_called()

    def test_core_and_ocr_installation_use_separate_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with (patch.dict(os.environ, {"JINGYAN_CONFIG": str(data / "config.json")}),
                  patch.object(bootstrap, "probe_python", return_value=True),
                  patch.object(bootstrap.subprocess, "run") as install,
                  patch.object(bootstrap.shutil, "which", return_value=None)):
                bootstrap.initialize(data)
                calls = install.call_args_list
                self.assertEqual(Path(calls[0].args[0][-1]).name, "requirements.lock")
                self.assertNotIn("rapidocr", calls[1].args[0][-1])
                bootstrap.initialize(data, profile="ocr")
                calls = install.call_args_list
                self.assertEqual(Path(calls[2].args[0][-1]).name, "requirements-ocr.lock")
                self.assertIn("rapidocr", calls[3].args[0][-1])

    def test_permission_failure_stops_without_trying_another_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with (patch.object(bootstrap, "probe_python", return_value=False),
                  patch.object(bootstrap.venv, "EnvBuilder") as builder,
                  patch.object(bootstrap.subprocess, "run") as install):
                builder.return_value.create.side_effect = PermissionError("synthetic restriction")
                with self.assertRaises(bootstrap.EnvironmentSetupError) as caught:
                    bootstrap.initialize(data)
            self.assertEqual(caught.exception.details()["error_code"], "environment_permission_required")
            install.assert_not_called()

    def test_partial_environment_repair_preserves_config_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "personal"
            broken = data / "venv"
            broken.mkdir(parents=True)
            (broken / "partial.txt").write_text("unfinished environment")
            credentials = root / "credentials.env"
            credentials.write_text("synthetic test credential")
            config = root / "config.json"
            config.write_text(json.dumps({"report_runtime": {"credentials_file": credentials.name}}))
            original = config.read_bytes()
            with patch.dict(os.environ, {"JINGYAN_CONFIG": str(config)}), \
                    patch.object(bootstrap, "probe_python", return_value=False), \
                    patch.object(bootstrap.venv, "EnvBuilder") as builder, \
                    patch.object(bootstrap.subprocess, "run"), \
                    patch.object(bootstrap.shutil, "which", return_value=None):
                result = bootstrap.initialize(data)
            builder.assert_called_once_with(with_pip=True, symlinks=os.name != "nt")
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(credentials.read_text(), "synthetic test credential")
            self.assertEqual(Path(result["config"]), config.resolve())
            self.assertFalse(result["model_setup_needed"])
            self.assertEqual((Path(result["previous_environment"]) / "partial.txt").read_text(),
                             "unfinished environment")

    def test_matching_marker_does_not_skip_missing_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            python = data / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
            marker = {"python": str(python), "uv": None,
                      "requirements_sha256": hashlib.sha256((bootstrap.PACKAGE / "requirements.lock").read_bytes()).hexdigest()}
            (data / "environment.json").write_text(json.dumps(marker))
            with patch.dict(os.environ, {"JINGYAN_CONFIG": str(data / "config.json")}), \
                    patch.object(bootstrap, "probe_python", side_effect=[True, False]), \
                    patch.object(bootstrap.subprocess, "run") as install, \
                    patch.object(bootstrap.shutil, "which", return_value=None):
                bootstrap.initialize(data)
            self.assertEqual(install.call_count, 2)

    @unittest.skipIf(os.name == "nt", "Unix entrypoint")
    def test_initializer_failure_propagates_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "fake-python"
            candidate.write_text('#!/bin/sh\necho "initialization failed" >&2\nexit 7\n')
            candidate.chmod(0o700)
            result = subprocess.run(["sh", str(ROOT / "project-report/scripts/run.sh"), "init"],
                                    env={**os.environ, "JINGYAN_HOME": str(root / "data"),
                                         "JINGYAN_PYTHON": str(candidate)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
