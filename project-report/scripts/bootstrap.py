#!/usr/bin/env python3
"""Prepare a private Python environment and a personal configuration once."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import venv

from model_setup import LOCAL_MODEL_CONFIG

PACKAGE = Path(__file__).resolve().parent.parent


DEPENDENCIES = {"core": ("docx", "pypdf"), "ocr": ("docx", "pypdf", "rapidocr", "onnxruntime", "pypdfium2")}


class EnvironmentSetupError(ValueError):
    def __init__(self, message, code, stage):
        super().__init__(message)
        self.code, self.stage = code, stage

    def details(self):
        return {"error_code": self.code, "retryable": False, "stage": self.stage,
                "next_action": "请确认本次目录、脚本及网络权限后继续；不要更换命令绕过限制或删除个人环境。"}


def setup_error(error, stage):
    reason = str(getattr(error, "stderr", "") or "")
    permission = isinstance(error, PermissionError) or any(text in reason for text in (
        "Permission denied", "Operation not permitted", "PermissionError", "Access is denied"))
    return EnvironmentSetupError("初始化需要目录或脚本访问权限，请确认权限后继续。" if permission else
                                 "初始化未完成，请检查网络及运行权限后从原入口继续。",
                                 "environment_permission_required" if permission else "environment_setup_failed", stage)


def probe_python(python, dependencies=False, profile="core"):
    code = 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))'
    if dependencies:
        code = ('import importlib.util; raise SystemExit(not all(importlib.util.find_spec(name) for name in '
                + repr(DEPENDENCIES[profile]) + '))')
    try:
        return subprocess.run([str(python), "-c", code], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def configuration_state(directory):
    config_file = Path(os.environ.get("JINGYAN_CONFIG", directory / "鲸研配置.json")).expanduser().resolve()
    # Optional credential paths have no role in the host workflow. In
    # particular, do not open a legacy config or follow its credential path.
    try:
        available = LOCAL_MODEL_CONFIG.is_file()
    except OSError:
        available = False
    result = {"config": str(config_file), "manual_model_config": str(LOCAL_MODEL_CONFIG),
              "manual_model_available": available, "manual_model_active": False,
              "model_setup_needed": False, "execution_mode": "host", "model_source": "host",
              "model": "WorkBuddy 当前会话模型", "api_mode_requires_explicit_selection": True,
              "optional_api_config_present": available}
    return result


def diagnose(directory):
    """Read-only: no mkdir, environment repair, pip, runtime copy or model call."""
    directory = directory.expanduser().resolve()
    python = directory / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
    environment = probe_python(python)
    core = environment and probe_python(python, dependencies=True)
    ocr = core and probe_python(python, dependencies=True, profile="ocr")
    return {"status": "ready" if core else "initialization_required", "read_only": True,
            "python": str(python), "core_ready": bool(core), "ocr_ready": bool(ocr),
            **configuration_state(directory)}


def initialize(directory, profile="core"):
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    environment = directory / "venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    lock = PACKAGE / ("requirements-ocr.lock" if profile == "ocr" else "requirements.lock")
    if not lock.exists():
        raise ValueError("技能包缺少依赖锁定文件，请重新解压完整发布包。")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    marker = directory / "environment.json"
    try:
        ready = json.loads(marker.read_text()) if marker.exists() else {}
    except (ValueError, OSError):
        ready = {}
    if not isinstance(ready, dict):
        ready = {}
    previous_environment = None
    if not probe_python(python):
        if environment.is_symlink() or (environment.exists() and not environment.is_dir()):
            raise ValueError("运行环境路径不是普通目录，请检查个人配置。")
        if environment.exists():
            previous_environment = environment.with_name("venv-incomplete-" + uuid.uuid4().hex[:10])
            environment.rename(previous_environment)
        # Managed macOS Python uses relative shared-library paths. Copying its
        # executable into the venv breaks dyld; the normal symlink layout works.
        try:
            venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(environment)
        except (OSError, subprocess.SubprocessError) as error:
            raise setup_error(error, "create_environment") from None
        ready = {}
    marker_key = "ocr_requirements_sha256" if profile == "ocr" else "requirements_sha256"
    if (ready.get(marker_key) != digest or ready.get("python") != str(python)
            or not probe_python(python, dependencies=True, profile=profile)):
        uv = os.environ.get("JINGYAN_UV") or ready.get("uv") or shutil.which("uv")
        command = [uv, "pip", "install", "--python", str(python)] if uv else [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
        try:
            subprocess.run([*command, "-r", str(lock)], check=True, stdout=sys.stderr, timeout=600)
            subprocess.run([str(python), "-c", "import " + ",".join(DEPENDENCIES[profile])], check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            raise setup_error(error, "install_" + profile) from None
        ready.update(python=str(python), uv=uv, **{marker_key: digest})
        if profile == "ocr":
            ready["requirements_sha256"] = hashlib.sha256((PACKAGE / "requirements.lock").read_bytes()).hexdigest()
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(ready, indent=2) + "\n")
        temporary.replace(marker)
    config_file = Path(os.environ.get("JINGYAN_CONFIG", directory / "鲸研配置.json")).expanduser().resolve()
    if not config_file.exists():
        config = json.loads((PACKAGE / "references/workspace-config.example.json").read_text())
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "initialized", "python": str(python), "dependency_profile": profile,
            **configuration_state(directory),
            **({"previous_environment": str(previous_environment)} if previous_environment else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("JINGYAN_HOME", Path.home() / ".jingyan")))
    parser.add_argument("--doctor", action="store_true", help="只读检查，不安装依赖")
    parser.add_argument("--ocr", action="store_true", help="安装扫描 PDF 使用的可选 OCR 依赖")
    args = parser.parse_args()
    try:
        result = diagnose(args.data_dir) if args.doctor else initialize(args.data_dir, "ocr" if args.ocr else "core")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        failure = error if isinstance(error, EnvironmentSetupError) else setup_error(error, "check_environment" if args.doctor else "initialize")
        print(json.dumps({"status": "initialization_failed", "error": str(failure), **failure.details()}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
