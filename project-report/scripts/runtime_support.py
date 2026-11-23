"""Materialize the packaged report engine in the user's writable workspace."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile

BUNDLE = Path(__file__).resolve().parent.parent / "runtime"


def runtime_files():
    return sorted(path for path in BUNDLE.rglob("*") if path.is_file()
                  and path.suffix in {".py", ".json", ".md", ".swift", ".txt"}
                  and "__pycache__" not in path.parts)


def prepare_runtime(workspace):
    files = runtime_files()
    if not (BUNDLE / "scripts/report_worker.py").is_file():
        raise ValueError("鲸研报告核心缺失，请重新解压完整技能包。")
    manifest = {str(path.relative_to(BUNDLE)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    key = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    parent = workspace / "_runtime"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / key[:24]
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=parent, prefix=".install-") as directory:
            staging = Path(directory) / "engine"
            for path in files:
                destination = staging / path.relative_to(BUNDLE)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            (staging / "package-files.json").write_text(json.dumps(manifest, indent=2) + "\n")
            try:
                staging.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    for relative, digest in manifest.items():
        path = target / relative
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("用户目录中的报告核心校验失败，请重新初始化鲸研环境。")
    return target
