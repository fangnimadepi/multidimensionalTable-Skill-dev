"""Private, source-free failure summaries and a local maintenance CLI."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import time
import traceback

from review_common import MODEL_KEY_NAMES, atomic_write_json

ROOT = Path(__file__).resolve().parent.parent


def configure_service_logging(run):
    """Persist stdout/stderr even when a Windows task uses pythonw.exe."""
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("project-assistant-service")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        run / "service.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    previous = sys.stdout, sys.stderr
    previous_raise = logging.raiseExceptions
    logging.raiseExceptions = (
        False  # Do not recurse through stderr if a disk write fails.
    )

    class Stream:
        encoding = "utf-8"

        def __init__(self, original, level):
            self.original, self.level = original, level

        def write(self, text):
            cleaned = safe_text(text, 8000)
            if self.original is not None:
                self.original.write(cleaned)
            if cleaned.strip():
                logger.log(self.level, cleaned.rstrip())
            return len(text)

        def flush(self):
            if self.original is not None:
                self.original.flush()
            handler.flush()

        def isatty(self):
            return False

    sys.stdout, sys.stderr = Stream(previous[0], logging.INFO), Stream(
        previous[1], logging.ERROR
    )

    def close():
        sys.stdout, sys.stderr = previous
        logger.removeHandler(handler)
        handler.close()
        logging.raiseExceptions = previous_raise

    return close


def safe_text(value, limit=2000):
    text = str(value)
    for name in (*MODEL_KEY_NAMES, 'PROJECT_ASSISTANT_ACCESS_KEY'):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\bsk-[A-Za-z0-9_.-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+[^\s,;\"\']+", "Bearer [REDACTED]", text)
    text = re.sub(r"https?://[^\s<>\"\']+", "[URL omitted]", text)
    return text[:limit]


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def safe_value(value):
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if key.lower()
                in {
                    "apikey",
                    "api_key",
                    "access_key",
                    "authorization",
                    "session_token",
                    "clientaccesshash",
                }
                else safe_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [safe_value(item) for item in value]
    return safe_text(value, 4000) if isinstance(value, str) else value


def exception_details(error):
    # Frames omit locals and source-code lines, which can contain credentials.
    frames = []
    for frame in traceback.extract_tb(error.__traceback__)[-16:]:
        file = Path(frame.filename)
        try:
            name = str(file.resolve().relative_to(ROOT))
        except ValueError:
            name = file.name
        frames.append({"file": name, "line": frame.lineno, "function": frame.name})
    result = {"type": type(error).__name__, "message": safe_text(error), "frames": frames}
    if type(error).__name__ == 'ModelRequestError':
        result.update(model_category=error.category, http_status=error.http_status, retryable=error.retryable)
    return result


def failure_details(folder, error, job=None):
    folder = Path(folder)
    job = job or {}
    progress = read_json(folder / "progress.json")
    timing = progress.get("timing") or {}
    manifest = read_json(folder / "manifest.json")
    failed = [
        {key: value.get(key) for key in ("report", "stage", "errorType", "time")}
        for value in timing.get("steps", [])
        if value.get("event") == "stage_failed"
    ][-6:]
    return {
        "schemaVersion": "report-failure-v1",
        "recordedAt": time.time(),
        "jobId": folder.name,
        "tableId": job.get("tableId"),
        "recordId": job.get("recordId"),
        "projectId": job.get("projectId"),
        "projectName": job.get("projectName"),
        "types": job.get("types") or manifest.get("types"),
        "model": job.get("selectedModel")
        or (manifest.get("modelConfig") or {}).get("text_model"),
        "provider": (manifest.get("modelConfig") or {}).get("provider"),
        "pipelineFingerprint": manifest.get("pipelineFingerprint"),
        "inputs": [
            {key: item.get(key) for key in ("role", "name", "size")}
            for item in job.get("inputFiles", [])
        ],
        "error_type": type(error).__name__,
        "error": safe_text(error),
        "exception": exception_details(error),
        "phase": progress.get("stage"),
        "failedStages": failed,
        "activeSteps": timing.get("activeSteps", []),
        "elapsedSeconds": timing.get("elapsedSeconds"),
        "completedCalls": timing.get("completedCalls"),
        "cachedSteps": timing.get("cachedSteps"),
        "worker": read_json(folder / "worker-failure.json"),
    }


def write_failure(path, value):
    """A failed diagnostic write must neither erase the error nor kill the queue."""
    try:
        atomic_write_json(path, safe_value(value))
        return True
    except OSError as error:
        fallback = {
            "event": "diagnostic_write_failed",
            "file": Path(path).name,
            "jobId": value.get("jobId"),
            "writeError": type(error).__name__,
            "originalError": value.get("error", value.get("message")),
        }
        print(
            json.dumps(safe_value(fallback), ensure_ascii=True),
            file=sys.stderr,
            flush=True,
        )
        return False


def collect(run, job_id=None, limit=10):
    run = Path(run).resolve()
    if job_id and not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise ValueError("任务编号应为16位十六进制字符。")
    if not run.is_dir():
        raise ValueError("运行目录不存在。")
    pattern = job_id or "*"
    paths = list(run.glob(f"jobs/{pattern}/job.json")) + list(
        run.glob(f"tables/*/jobs/{pattern}/job.json")
    )
    if job_id and not paths:
        raise ValueError("未找到该任务编号。")
    jobs, warnings = [], []
    for path in paths:
        if not path.resolve().is_relative_to(run):
            continue
        job = read_json(path)
        if not job:
            warnings.append(str(path.relative_to(run)))
            continue
        if not job_id and job.get("status") != "failed":
            continue
        failure = read_json(path.parent / "failure.json")
        jobs.append(
            {
                **{
                    key: job.get(key)
                    for key in (
                        "id",
                        "tableId",
                        "recordId",
                        "projectId",
                        "projectName",
                        "status",
                        "message",
                        "types",
                        "startedAt",
                        "completedAt",
                        "failedAt",
                        "publishedAt",
                    )
                },
                "inputs": [
                    {key: value.get(key) for key in ("role", "name", "size")}
                    for value in job.get("inputFiles", [])
                ],
                "folder": str(path.parent.relative_to(run)),
                "failure": failure,
                "worker": read_json(path.parent / "worker-failure.json"),
                "timing": {
                    key: read_json(path.parent / "timing-summary.json").get(key)
                    for key in (
                        "elapsedSeconds",
                        "completedCalls",
                        "cachedSteps",
                        "apiWallSumSeconds",
                    )
                },
            }
        )
    jobs.sort(
        key=lambda value: value.get("failedAt") or value.get("startedAt") or 0,
        reverse=True,
    )
    events = list(run.glob("events/*.json")) + list(run.glob("tables/*/events/*.json"))
    ui = []
    if not job_id:
        for path in sorted(events, key=lambda p: p.name, reverse=True):
            if not path.resolve().is_relative_to(run):
                continue
            value = read_json(path)
            if value.get("stage") == "generation-failed":
                # Older UI events used a plain message; newer events include jobId.
                ui.append(
                    {
                        "file": str(path.relative_to(run)),
                        **{
                            key: value.get(key)
                            for key in ("stage", "tableId", "recordId", "message")
                        },
                    }
                )
                if len(ui) >= limit:
                    break
    # Default export excludes prompts, source excerpts, downloads, credentials
    # and restoration packages. Detailed business material remains on server.
    api = []
    if not job_id:
        for path in list(run.glob("api-failures/*.json")) + list(
            run.glob("tables/*/api-failures/*.json")
        ):
            if not path.resolve().is_relative_to(run):
                continue
            value = read_json(path)
            if value:
                api.append({"file": str(path.relative_to(run)), **value})
        api.sort(key=lambda value: value.get("recordedAt", 0), reverse=True)
    uploads = []
    if not job_id:
        for path in list(run.glob("upload-records/*.json")) + list(
            run.glob("tables/*/upload-records/*.json")
        ):
            if not path.resolve().is_relative_to(run):
                continue
            value = read_json(path)
            if value:
                uploads.append(
                    {
                        key: value.get(key)
                        for key in (
                            "uploadId",
                            "tableId",
                            "recordId",
                            "projectName",
                            "startedAt",
                            "endedAt",
                            "status",
                            "files",
                            "metrics",
                            "clientTiming",
                            "validationSeconds",
                            "error",
                        )
                    }
                )
        uploads.sort(
            key=lambda value: value.get("endedAt") or value.get("startedAt") or 0,
            reverse=True,
        )
    return safe_value(
        {
            "jobs": jobs[:limit],
            "uiFailures": ui,
            "apiFailures": api[:limit],
            "uploads": uploads[:limit],
            "unreadableJobFiles": warnings,
        }
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("limit须为1至100。")
    run = args.run_dir
    if run is None:
        config = read_json(ROOT / "config/server.local.json")
        run = ROOT / config.get("run_dir", "runs/s")
    try:
        result = collect(run, args.job_id, args.limit)
    except ValueError as error:
        parser.error(str(error))
    # UI/legacy errors are untrusted text; sanitize the export as well.
    result = safe_value(result)
    if args.output:
        # Round-trip preserves JSON escaping after credential replacement.
        atomic_write_json(args.output, result)
        print("诊断摘要已保存：" + str(args.output))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
