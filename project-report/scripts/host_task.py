#!/usr/bin/env python3
"""Submit one host response through the managed runtime; no ad hoc validators."""

import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

# Direct invocation is the normal entrypoint. Prepare packaged imports before
# importing host_reports; never depend on a parent agent's PYTHONPATH or CWD.
RUNTIME_SCRIPTS = Path(__file__).resolve().parents[1] / "runtime" / "scripts"
sys.path.insert(0, str(RUNTIME_SCRIPTS))

from host_materials import digest
from host_protocol import HostTaskPending
from host_tasks import pending_result
from local_reports import load_json, setup_host_runtime, workspace
from read_docx import inside


def load_task(request_file, config_file, config):
    root = workspace(config_file, config)
    path = inside(Path(request_file), root)
    if not path.is_file() or not path.name.startswith("request-") or path.suffix != ".json" or path.parent.parent.name != "_host":
        raise ValueError("请使用当前待办返回的 request_file，不能提交其他文件。")
    request = load_json(path)
    state = load_json(path.parent / "state.json")
    if state.get("content_sha256") != digest({k: v for k, v in state.items() if k != "content_sha256"}):
        raise ValueError("宿主任务状态不一致。")
    continuation = request.get("continuation")
    if not isinstance(continuation, dict) or continuation != state.get("continuation"):
        raise ValueError("续跑入口与已保存任务不一致。")
    if Path(continuation["config_file"]).resolve() != config_file:
        raise ValueError("待办不属于当前个人工作区配置。")
    response = inside(Path(request["response_file"]), path.parent)
    pending = state.get("pending")
    if state.get("status") == "working":
        if not pending or Path(pending["request_file"]).resolve() != path:
            raise ValueError("此待办已经提交，请使用最新回执的 next_command；不要重复提交旧待办。")
        if digest(request) != pending["request_sha256"]:
            raise ValueError("待办内容发生变化。")
        if not response.is_file():
            raise ValueError("响应文件尚未创建；请先用文件写入工具填写 response_file，再执行提交命令。无需轮询或编写脚本。")
    return root, path, request, state, response


def submit(request_file, config_file, config):
    root, path, request, state, response = load_task(request_file, config_file, config)
    continuation = request["continuation"]
    from host_reports import VERSION, fingerprint
    if state.get("version") != VERSION or state.get("workflow_fingerprint") != fingerprint():
        raise ValueError("技能版本或规则已更新；当前旧待办已停止，请用新版入口建立本次任务。原材料和旧结果均保留。")
    if continuation["kind"] == "feishu":
        from feishu_batch import run, Connector
        batch_file = inside(Path(continuation["batch"]), root / "_batches")
        batch = load_json(batch_file)
        if batch.get("execution_mode") != "host":
            raise ValueError("宿主提交入口只能继续宿主模型批次，不能切换到独立 API。")
        if path.parent.parent.parent.name != batch_file.parent.name:
            raise ValueError("待办与飞书批次不一致。")
        options = SimpleNamespace(batch=batch_file, limit=None, retry_failed=False, response=response)
        return run(Connector(Path.cwd().resolve()), config_file, config, root, options)
    if continuation["kind"] != "local":
        raise ValueError("未知宿主续跑类型。")
    run_dir = inside(Path(continuation["run_dir"]), root)
    if path.parent.parent.parent != run_dir:
        raise ValueError("待办与本地项目运行目录不一致。")
    runtime, model = setup_host_runtime(config_file, config)
    options = SimpleNamespace(**{key: continuation.get(key) for key in
                              ("types", "taskbook", "report", "record_id", "project_name")},
                              response=response, continuation=continuation)
    from local_reports import generate
    try:
        return generate(options, runtime, run_dir, config, model)
    except HostTaskPending as pending:
        return pending_result(pending.task)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("action", choices=("submit",))
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    try:
        config_file = args.config.expanduser().resolve(strict=True)
        os.environ["JINGYAN_CONFIG"] = str(config_file)
        result = submit(args.request, config_file, load_json(config_file))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError) as error:
        result = {"status": "failed", "message": str(error), "retryable": False,
                  **(error.details() if callable(getattr(error, "details", None)) else {}),
                  "next_action": "按具体反馈修正当前响应；不要研究源码、自建校验器或重复运行相同命令。"}
    print(json.dumps(result, ensure_ascii=False))
    return {"needs_model": 0, "continue_batch": 0, "completed": 0, "checked": 0,
            "incomplete": 3, "needs_selection": 3}.get(result.get("status"), 2)


if __name__ == "__main__":
    sys.exit(main())
