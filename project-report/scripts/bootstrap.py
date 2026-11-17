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


def probe_python(python, dependencies=False):
    code = 'import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,14)))'
    if dependencies:
        code = ('import importlib.util; '
                'raise SystemExit(not all(importlib.util.find_spec(name) for name in '
                '("docx","pypdf","rapidocr","onnxruntime","pypdfium2")))')
    try:
        return subprocess.run([str(python), "-c", code], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def initialize(directory):
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    environment = directory / "venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    lock = PACKAGE / "requirements.lock"
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
        venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(environment)
        ready = {}
    if (ready.get("requirements_sha256") != digest or ready.get("python") != str(python)
            or not probe_python(python, dependencies=True)):
        uv = os.environ.get("JINGYAN_UV") or ready.get("uv") or shutil.which("uv")
        command = [uv, "pip", "install", "--python", str(python)] if uv else [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
        subprocess.run([*command, "-r", str(lock)], check=True, stdout=sys.stderr)
        subprocess.run([str(python), "-c", "import docx,pypdf,rapidocr,onnxruntime,pypdfium2"], check=True)
        ready = {"python": str(python), "requirements_sha256": digest, "uv": uv}
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(ready, indent=2) + "\n")
        temporary.replace(marker)
    config_file = Path(os.environ.get("JINGYAN_CONFIG", directory / "鲸研配置.json")).expanduser().resolve()
    if not config_file.exists():
        config = json.loads((PACKAGE / "references/workspace-config.example.json").read_text())
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    credentials = Path(config.get("report_runtime", {}).get("credentials_file", "local/jingyan-model.env")).expanduser()
    if not credentials.is_absolute():
        credentials = config_file.parent / credentials
    return {"status": "initialized", "python": str(python), "config": str(config_file),
            "manual_model_config": str(LOCAL_MODEL_CONFIG), "manual_model_active": LOCAL_MODEL_CONFIG.is_file(),
            "model_setup_needed": not LOCAL_MODEL_CONFIG.is_file() and not credentials.is_file(),
            **({"previous_environment": str(previous_environment)} if previous_environment else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("JINGYAN_HOME", Path.home() / ".jingyan")))
    args = parser.parse_args()
    try:
        result = initialize(args.data_dir)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "initialization_failed", "error": str(error),
                          "action": "检查网络和运行权限后重新初始化，已下载依赖可复用。"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
