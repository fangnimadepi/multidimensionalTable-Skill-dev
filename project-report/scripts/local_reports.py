#!/usr/bin/env python3
"""Read project documents and export checked Word reports using the packaged engine."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from read_docx import extract, file_hash, inside
from host_protocol import HostTaskPending
from host_tasks import pending_result


class WorkerFailure(ValueError):
    def __init__(self, message, directory, *, error_code="report_worker_failed", retryable=False,
                 validation_stage=None, validation_file=None):
        super().__init__(message)
        self.directory = directory
        self.error_code = error_code
        self.retryable = retryable
        self.validation_stage = validation_stage
        self.validation_file = validation_file

    def details(self):
        actions = {"model_balance": "请在模型平台充值或更换已配置模型，再从原批次继续。",
                   "model_authentication": "请在模型设置中核对密钥，再从原批次继续。",
                   "model_forbidden": "请核对模型访问权限，再从原批次继续。",
                   "model_tls": "请检查网络及接口证书配置。",
                   "model_response_format": "请将错误码和诊断目录交给维护人员检查模型接口格式。"}
        return {"diagnostics": str(self.directory), "error_code": self.error_code,
                "retryable": self.retryable,
                **({"validation_stage": self.validation_stage} if self.validation_stage else {}),
                **({"validation_file": str(self.validation_file)} if self.validation_file else {}),
                "next_action": actions.get(self.error_code, "临时连接或等待失败，可用原批次重试一次。" if self.retryable else
                                           "本项目已停止。将错误码和诊断目录交给维护人员，修复后继续。")}


def worker_failure(directory, error):
    if isinstance(error, subprocess.TimeoutExpired):
        return WorkerFailure("报告处理超过等待时限，已保留完成步骤。", directory,
                             error_code="report_timeout", retryable=True)
    try:
        detail = load_json(directory / "worker-failure.json")
        error_type, message = detail.get("type", detail.get("error_type", "")), str(detail.get("message") or "")
    except (OSError, ValueError, AttributeError):
        error_type, message, detail = "", "", {}
    if error_type == "ModelRequestError":
        category = detail.get("model_category")
        if category not in {"balance", "authentication", "forbidden", "rate_limit", "server", "tls", "timeout", "connection", "response_format", "http"}:
            category = "http"
        labels = {"balance": "模型账号余额不足，请充值或更换模型。", "authentication": "模型密钥验证失败，请核对设置。",
                  "forbidden": "当前账号无模型访问权限。", "response_format": "模型响应格式不完整或不兼容。",
                  "tls": "模型接口的安全连接验证失败。"}
        return WorkerFailure(labels.get(category, "模型请求未完成，已保留材料和完成步骤。"), directory,
                             error_code="model_" + category,
                             retryable=category in {"rate_limit", "server", "timeout", "connection"})
    if error_type in {"ValueError", "ReportValidationError"}:
        stage_messages = {
            "review/initial": "履约报告初稿未通过正文或成果认定口径检查。",
            "review/final": "履约报告最终综合意见、确认话术或依据用途检查未通过。",
            "review/artifacts": "履约报告最终文件的来源绑定或结构检查未通过。",
            "achievement/facts": "成果报告部分表述未通过事实或来源检查。",
            "achievement/artifacts": "成果报告最终文件的事实绑定或结构检查未通过。",
        }
        stage = detail.get("validation_stage")
        feedback_file = detail.get("validation_file")
        if not isinstance(feedback_file, str) or not Path(feedback_file).resolve().is_relative_to(directory.parent.resolve()):
            feedback_file = None
        return WorkerFailure(stage_messages.get(stage, "报告正文或来源检查未通过，已停止本项目并保留诊断记录。"),
                             directory, error_code="report_validation_failed",
                             validation_stage=stage if stage in stage_messages else None, validation_file=feedback_file)
    retryable = error_type in {"TimeoutError", "ConnectionError", "URLError"} or (
        error_type == "RuntimeError" and any(value in message for value in (
            "TimeoutError", "URLError", "RemoteDisconnected", "ConnectionError", "HTTP 429", "HTTP 500",
            "HTTP 502", "HTTP 503", "HTTP 504")))
    return WorkerFailure("报告处理未完成，已停止本项目并保留诊断记录。", directory,
                         error_code="model_connection_failed" if retryable else "report_worker_failed",
                         retryable=retryable)


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def workspace(config_file, config):
    return (config_file.parent / config.get("workspace_root", "鲸研工作区")).resolve()


def setup_runtime(config_file, config, use_credentials=True):
    runtime = config.get("report_runtime", {})
    from runtime_support import prepare_runtime

    root = prepare_runtime(workspace(config_file, config))
    scripts = root / "scripts"
    sys.path.insert(0, str(scripts))
    from llm_runtime import model_config
    from model_setup import credential_path, load_credentials, manual_model_options
    from workbuddy_models import model_file, model_options as workbuddy_options
    from private_model_settings import PROVIDERS, _supported

    ocr_engine = runtime.get("ocr_engine")
    if ocr_engine:
        if ocr_engine not in {"vision", "rapidocr"}:
            raise ValueError("OCR 引擎应为 vision 或 rapidocr。")
        os.environ["PROJECT_ASSISTANT_OCR_ENGINE"] = ocr_engine

    source = runtime.get("model_source", "auto")
    if source not in {"auto", "workbuddy", "configured"}:
        raise ValueError("模型来源应为 auto、workbuddy 或 configured。")
    if source == "workbuddy":
        return root, {**model_config(), **workbuddy_options(runtime, PROVIDERS, use_credentials)}
    manual = manual_model_options(PROVIDERS, use_credentials)
    if manual is not None:
        return root, {**model_config(), **manual, "model_source": "manual"}
    if source == "auto" and not credential_path(config_file, runtime).is_file() and model_file() is not None:
        return root, {**model_config(), **workbuddy_options(runtime, PROVIDERS, use_credentials)}

    provider = runtime.get("provider")
    model = runtime.get("model")
    if provider not in PROVIDERS or not _supported(provider, model):
        raise ValueError("请在鲸研配置中指定受支持的报告模型平台和模型。")
    _, endpoint, key_env, _ = PROVIDERS[provider]
    model_options = {**model_config(), "provider": provider, "api_base": endpoint,
                     "api_key_env": key_env, "text_model": model, "model_source": "configured"}
    if use_credentials:
        load_credentials(config_file, runtime, key_env)
    return root, model_options


def setup_host_runtime(config_file, config):
    """Prepare parsers/rules only, with no model config or credential lookup."""
    from runtime_support import prepare_runtime
    root = prepare_runtime(workspace(config_file, config))
    sys.path.insert(0, str(root / "scripts"))
    runtime = config.get("report_runtime")
    engine = runtime.get("ocr_engine", "rapidocr") if isinstance(runtime, dict) else "rapidocr"
    if engine not in {"rapidocr", "vision"}:
        raise ValueError("OCR 引擎应为 rapidocr 或 vision。")
    os.environ["PROJECT_ASSISTANT_OCR_ENGINE"] = engine
    return root, {"execution_mode": "host", "model_source": "host", "provider": "host",
                  "text_model": "WorkBuddy 当前会话模型"}


def source_path(run_dir, relative_input, role, config):
    role_dir = inside(run_dir / "raw" / role, run_dir)
    if Path(relative_input).is_absolute() or ".." in Path(relative_input).parts:
        raise ValueError("材料路径须为本次运行目录下的相对路径。")
    path = inside(run_dir / relative_input, role_dir)
    allowed = {".docx"} if role == "taskbook" else {".docx", ".pdf"}
    if path.suffix.lower() not in allowed:
        raise ValueError("任务书使用 DOCX，阶段报告使用 DOCX 或 PDF。旧版 DOC 请先转成 DOCX。")
    if not path.is_file() or not 0 < path.stat().st_size <= config.get("max_file_mib", 128) * 1024**2:
        raise ValueError(f"材料 {role} 缺失、为空或超过单文件大小限制。")
    return path


def read_material(run_dir, path, role):
    from material_io import validate_document
    from review_common import atomic_write_json

    validate_document(path, path.suffix.lower())
    if path.suffix.lower() == ".docx":
        return extract(run_dir, path.relative_to(run_dir), role)
    ensure_pdf_ocr(path)
    from pdf_ocr import read_pdf_pages

    digest = file_hash(path)
    pages, ocr = read_pdf_pages(path)
    if file_hash(path) != digest:
        raise ValueError("PDF 在读取期间发生变化，请重新核对材料。")
    output = inside(run_dir / "extracted" / f"{role}.json", run_dir)
    summary = {"text_extraction": "succeeded", "role": role, "sha256": digest,
               "json_path": str(output), "ocr": ocr}
    scanned = set(ocr["ocr_pages"])
    atomic_write_json(output, {**summary, "pages": [
        {"page": index, "source": "ocr" if index in scanned else "native",
         "has_text": bool(content.strip()), "text": content}
        for index, content in enumerate(pages, 1)
    ]})
    return summary


def ensure_pdf_ocr(path):
    """Install the optional local OCR profile only for pages needing OCR."""
    from pdf_ocr import backend_name, capabilities, inspect_pdf

    _, needed = inspect_pdf(path)
    if not needed or backend_name() != "rapidocr" or capabilities()["available"]:
        return
    from bootstrap import EnvironmentSetupError, initialize
    environment = Path(sys.prefix)
    if environment.name != "venv" or sys.prefix == sys.base_prefix:
        raise EnvironmentSetupError("扫描 PDF 需要 OCR 组件，请用技能入口执行 init --ocr 后继续。",
                                    "ocr_initialization_required", "install_ocr")
    print(json.dumps({"step": "首次准备扫描 PDF 识别组件，后续复用", "ocr_pages": len(needed)},
                     ensure_ascii=False), file=sys.stderr, flush=True)
    initialize(environment.parent, profile="ocr")
    import importlib
    importlib.invalidate_caches()
    ready = capabilities()
    if not ready["available"]:
        raise EnvironmentSetupError("OCR 组件尚未就绪，请将初始化记录交给维护人员。",
                                    "ocr_initialization_failed", "install_ocr")


def snapshot_inputs(work, paths, hashes):
    staged = {}
    for role, source in paths.items():
        target = inside(work / "inputs" / role / source.name, work)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copyfile(source, temporary)
                if file_hash(temporary) != hashes[role]:
                    raise ValueError("材料在复制期间发生变化，请重新核对附件。")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        if file_hash(target) != hashes[role]:
            raise ValueError("本地材料副本校验失败，请使用新的运行目录。")
        staged[role] = str(target)
    return staged


def checked_reports(result, types, hashes, fingerprint):
    required = set(types) | ({"paired"} if len(types) == 2 else set())
    if (result.get("materialHashes") != hashes or result.get("pipelineFingerprint") != fingerprint
            or set(result.get("reports", {})) != set(types)
            or any(result.get("checks", {}).get(kind, {}).get("passed") is not True for kind in required)):
        raise ValueError("出稿未通过材料版本或报告检查，停止归档。")
    return result["reports"]


@contextmanager
def exclusive_work(work):
    """Reuse completed steps without two workers writing the same step artifacts."""
    work.mkdir(parents=True, exist_ok=True)
    with inside(work / ".lock", work).open("a+b") as lock:
        try:
            if sys.platform == "win32":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("本次项目已有生成任务，请等待完成后再运行。") from error
        yield


def generate(args, root, run_dir, config, model):
    if model.get("execution_mode") == "host":
        from host_reports import advance
        return advance(args, root, run_dir, config)
    from report_worker import pipeline_fingerprint
    from feishu_report_bundle import REPORT_TITLES
    from reading_copy import compose
    from word_reports import render_docx
    from material_io import validate_document
    from pdf_ocr import backend_name
    from review_common import api_key, atomic_write_json

    started = time.monotonic()
    types = args.types
    if len(set(types)) != len(types) or ("forecast" in types and types != ["forecast"]):
        raise ValueError("预期成果分析须单独生成，每类报告只能选一次。")
    if (types == ["forecast"]) == bool(args.report):
        raise ValueError("预期成果分析只使用任务书，阶段报告需要配对的总结报告。")
    paths = {"plan": source_path(run_dir, args.taskbook, "taskbook", config)}
    if args.report:
        paths["report"] = source_path(run_dir, args.report, "report", config)
    if sum(path.stat().st_size for path in paths.values()) > config.get("max_project_mib", 192) * 1024**2:
        raise ValueError("材料合计超过项目大小限制。")
    for path in paths.values():
        validate_document(path, path.suffix.lower())
        if path.suffix.lower() == ".pdf":
            ensure_pdf_ocr(path)
    if not api_key(model["api_key_env"]):
        raise ValueError(f"本地报告模型缺少 {model['api_key_env']}，请在运行库配置后重试。")
    hashes = {role: file_hash(path) for role, path in paths.items()}
    fingerprint = pipeline_fingerprint()
    context = [str(run_dir), {role: str(path) for role, path in paths.items()}, hashes, fingerprint, model]
    if "report" in paths and paths["report"].suffix.lower() == ".pdf":
        context.append(backend_name())
    key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    work = inside(root / "runs" / "workbuddy" / key, root / "runs")
    with exclusive_work(work):
        staged = snapshot_inputs(work, paths, hashes)
        attempt = Path(tempfile.mkdtemp(dir=work, prefix="attempt-"))
        manifest = attempt / "manifest.json"
        atomic_write_json(manifest, {"types": types, "workDir": str(work), "paths": staged,
                                    "hashes": hashes, "pipelineFingerprint": fingerprint,
                                    "modelConfig": model, "recordId": args.record_id or run_dir.parent.name,
                                    "projectName": args.project_name, "attemptId": attempt.name})
        try:
            with (attempt / "worker.log").open("wb") as log:
                subprocess.run([sys.executable, str(root / "scripts" / "report_worker.py"), str(manifest)],
                               cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise worker_failure(attempt, error) from error
    result = load_json(attempt / "generated.json")
    markdown = checked_reports(result, types, hashes, fingerprint)
    output_root = inside(run_dir / "reports", run_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / attempt.name
    with tempfile.TemporaryDirectory(dir=output_root, prefix=".export-") as temporary:
        draft = Path(temporary) / attempt.name
        draft.mkdir()
        documents = {}
        for kind, full_markdown in markdown.items():
            structured = None
            entry = result.get("reportFiles", {}).get(kind)
            if entry:
                snapshot = inside(root / entry["path"], root / "runs")
                if file_hash(snapshot) != entry["sha256"]:
                    raise ValueError("已核对的报告底稿校验失败。")
                structured = load_json(snapshot)
            body, appendix, card = compose(kind, full_markdown, structured)
            content = body.rstrip() + "\n\n" + appendix
            md = draft / (REPORT_TITLES[kind] + ".md")
            word = md.with_suffix(".docx")
            md.write_text(content, encoding="utf-8")
            stats = render_docx(content, word, title=REPORT_TITLES[kind], card=card)
            documents[kind] = {"docx": str(output / word.name), "markdown": str(output / md.name),
                               "sha256": stats["sha256"], "bytes": stats["bytes"], "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               "internal_links": stats["internal_links"], "bookmarks": stats["bookmarks"], "card": stats["card"]}
        if any(file_hash(path) != hashes[role] for role, path in paths.items()):
            raise ValueError("原材料在生成期间发生变化，停止发布。")
        metadata = result["runMetadata"]
        receipt = {"status": "checked", "record_id": args.record_id, "project_id": result["projectId"],
                   "material_hashes": hashes, "checks": result["checks"], "documents": documents,
                   "model": metadata["model"], "provider": metadata["provider"],
                   "new_model_calls": metadata["newModelCalls"], "seconds": round(time.monotonic() - started, 2),
                   "diagnostics": str(attempt), "receipt": str(output / "checked.json")}
        atomic_write_json(draft / "checked.json", receipt)
        draft.rename(output)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("check-model", help="向已配置模型发送一条最小请求")
    read = sub.add_parser("read")
    read.add_argument("--role", required=True, choices=("taskbook", "report"))
    read.add_argument("--input", required=True)
    build = sub.add_parser("generate")
    build.add_argument("--taskbook", required=True)
    build.add_argument("--report")
    build.add_argument("--types", required=True, nargs="+", choices=("review", "achievement", "forecast"))
    build.add_argument("--project-name")
    build.add_argument("--record-id")
    build.add_argument("--mode", choices=("host", "api"), default="host",
                       help="默认使用宿主会话模型；仅明确选择 api 时读取独立模型配置")
    build.add_argument("--response", type=Path, help="宿主按待办写好的 JSON 响应文件")
    for command in (read, build):
        command.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        config_file = args.config.expanduser().resolve(strict=args.command != "doctor")
        os.environ["JINGYAN_CONFIG"] = str(config_file)
        if args.command == "doctor":
            from bootstrap import diagnose
            directory = Path(os.environ.get("JINGYAN_HOME", Path(sys.prefix).parent if Path(sys.prefix).name == "venv" else Path.home() / ".jingyan"))
            print(json.dumps(diagnose(directory), ensure_ascii=False))
            return 0
        config = load_json(config_file)
        use_api = args.command == "check-model" or args.command == "generate" and args.mode == "api"
        if args.command == "generate" and args.mode == "api" and args.response:
            raise ValueError("API 模式不接收宿主响应文件。")
        root, model = setup_runtime(config_file, config) if use_api else setup_host_runtime(config_file, config)
        if args.command == "check-model":
            from model_setup import LOCAL_MODEL_CONFIG, verify_connection
            from review_common import api_key

            key = api_key(model["api_key_env"])
            active_config = model.get("model_config_file") or (LOCAL_MODEL_CONFIG if model.get("manual_endpoint") else config_file)
            result = {"status": "ready" if key else "model_key_missing", "model": model["text_model"],
                      "provider": model["provider"], "model_config_file": str(active_config),
                      "model_source": model["model_source"]}
            if key:
                verify_connection(model, key)
                result["connection_verified"] = True
        else:
            workspace_root = workspace(config_file, config)
            run_dir = inside(args.run_dir, workspace_root)
            if not run_dir.is_dir() or run_dir == workspace_root:
                raise ValueError("请指定工作区内已准备好的独立项目运行目录。")
            if args.command == "read":
                path = source_path(run_dir, args.input, args.role, config)
                result = read_material(run_dir, path, args.role)
            else:
                args.continuation = {"kind": "local", "config_file": str(config_file), "run_dir": str(run_dir),
                                     "taskbook": args.taskbook, "report": args.report, "types": args.types,
                                     "project_name": args.project_name, "record_id": args.record_id}
                result = generate(args, root, run_dir, config, model)
    except HostTaskPending as pending:
        model_task = pending.task
        resume = ["generate", "--run-dir", str(args.run_dir), "--taskbook", args.taskbook,
                  "--types", *args.types, "--response", model_task["response_file"]]
        for flag, value in (("--report", args.report), ("--project-name", args.project_name), ("--record-id", args.record_id)):
            if value:
                resume += [flag, value]
        print(json.dumps(pending_result(model_task, resume), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, ImportError, RuntimeError, TypeError, SyntaxError) as error:
        detail = {"status": "failed", "error": str(error)}
        if isinstance(error, WorkerFailure):
            detail.update(error.details())
        elif callable(getattr(error, "details", None)):
            detail.update(error.details())
        print(json.dumps(detail, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 3 if result.get("text_extraction") == "no_text" or result.get("status") == "model_key_missing" else 0


if __name__ == "__main__":
    sys.exit(main())
