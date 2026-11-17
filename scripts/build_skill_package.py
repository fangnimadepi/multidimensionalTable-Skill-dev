"""Build a standalone Skill archive from an explicit source-file allowlist."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "project-report"
EXTENSIONS = {".md", ".py", ".json", ".swift", ".txt", ".lock", ".sh", ".ps1"}
EXCLUDED = {"__pycache__", "local", "runs", "raw", "extracted", "reports", ".venv"}


def build():
    destination = ROOT / "dist/project-report.zip"
    destination.parent.mkdir(exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    with ZipFile(temporary, "w", ZIP_DEFLATED) as package:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and path.suffix in EXTENSIONS and not EXCLUDED.intersection(path.relative_to(SOURCE).parts):
                if path.is_symlink():
                    raise ValueError("发布包源文件不使用符号链接。")
                package.write(path, path.relative_to(ROOT))
    with ZipFile(temporary) as package:
        required = {"project-report/SKILL.md", "project-report/requirements.lock", "project-report/scripts/run.sh",
                    "project-report/scripts/run.ps1", "project-report/scripts/feishu_batch.py",
                    "project-report/scripts/feishu_connector.py", "project-report/runtime/scripts/report_worker.py",
                    "project-report/templates/document-layout.json"}
        if package.testzip() or not required <= set(package.namelist()):
            raise ValueError("发布包完整性检查失败。")
    temporary.replace(destination)
    print(destination)


if __name__ == "__main__":
    build()
