"""Host-facing task files and commands; no model requests or credentials."""

import json
import os
from pathlib import Path
import shlex
import sys

from host_prompts import PACKAGE


def command(argv, *, windows=None):
    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        return "& " + " ".join("'" + str(value).replace("'", "''") + "'" for value in argv)
    return shlex.join([str(value) for value in argv])


def submit_command(request_file, config_file):
    return command([sys.executable, PACKAGE / "scripts/host_task.py", "--config", config_file,
                    "submit", "--request", request_file])


def repair_details(errors, result, facts=None):
    """Resolve JSON paths for the host instead of making it count array rows."""
    plans = {p["item_id"]: p for p in (facts or {}).get("plan_items", [])}
    actual = {p["fact_id"]: p for p in (facts or {}).get("facts", [])}
    details = []
    for error in errors:
        path, _, message = error.partition(":")
        current, owner = result, {}
        for key in path.split("/"):
            if isinstance(current, dict):
                owner = current
                current = current.get(key)
            elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
                current = current[int(key)]
                if isinstance(current, dict):
                    owner = current
            else:
                break
        item_id = owner.get("item_id")
        plan = plans.get(item_id, {}) if isinstance(item_id, str) else {}
        info = {"path": path, "message": message, "index_base": 0}
        if plan:
            info.update(item_id=item_id, title=plan["title"], scope=plan["scope"], status=owner.get("status"))
        if isinstance(current, str):
            info["current_text"] = current[:2000]
        ids = owner.get("fact_ids", [])
        info["selected_facts"] = [{"fact_id": key, "kind": actual[key]["kind"], "text": actual[key]["text"]}
                                  for key in ids if isinstance(key, str) and key in actual] if isinstance(ids, list) else []
        if not plan and path.startswith("review/findings/"):
            parts = path.split("/")
            review = result.get("review") if isinstance(result, dict) else None
            rows = review.get("findings", []) if isinstance(review, dict) else []
            rows = rows if isinstance(rows, list) else []
            if len(parts) > 2 and parts[2].isdigit() and int(parts[2]) < len(rows) and isinstance(rows[int(parts[2])], dict):
                title = rows[int(parts[2])].get("title")
                info["title"] = title.get("text", "") if isinstance(title, dict) else title
        details.append(info)
    return details


def reading_task(content):
    """A single readable handoff; source text appears only once."""
    lines = ["# 鲸研当前待办", "阶段：" + content["stage"],
             "项目：" + str(content.get("project_name") or ""),
             "审查范围：" + json.dumps(content.get("review_scope", {}), ensure_ascii=False),
             "写入目标：" + content["response_file"], "", "提交命令：", "```", content["submit_command"], "```", ""]
    if content.get("submit_bash"):
        lines += ["若执行工具是 Windows Git Bash，直接使用这一条：", "```", content["submit_bash"], "```", ""]
    lines += ["## 当前要求", content["instructions"], "", "## 响应字段示例", "```json",
              json.dumps(content["response_envelope"], ensure_ascii=False, indent=2), "```"]
    for name in ("revision_feedback", "format_feedback", "allowed_roots", "fields", "available_sources", "required_plan_sources", "groups", "report_view"):
        if name in content:
            lines += ["", "## " + name, "```json", json.dumps(content[name], ensure_ascii=False, indent=2), "```"]
    if content.get("source_files"):
        lines += ["", "## 原材料分段", "以下文件共同构成本次原文，请全部读取；不是额外的模型阶段。"]
        lines += ["- " + file for file in content["source_files"]]
    else:
        lines += source_lines(content.get("source_context", []))
    for name, guide in content.get("writing_guides", {}).items():
        lines += ["", "## " + name, guide]
    return "\n".join(lines) + "\n"


def source_lines(items):
    lines = []
    for source in items:
        lines += ["", "### " + source["id"] + " | " + source["side"] + " | " + source["role"] + (" | " + source["section"] if source.get("section") else ""), source["text"]]
        if source.get("context"):
            lines += ["表格语境：" + source["context"]]
    return lines


def pending_result(model_task, resume_arguments=None):
    public = {key: model_task[key] for key in ("request_id", "stage", "stage_part", "request_file", "task_file", "response_file",
              "revision_attempt", "submit_command", "submit_bash") if key in model_task}
    return {"status": "needs_model", "execution_mode": "host", "model_task": public,
            "next_command": model_task["submit_command"],
            **({"next_command_bash": model_task["submit_bash"]} if model_task.get("submit_bash") else {}),
            "next_action": "只读 task_file（包含字段示例），写 response_file，执行 next_command；修订只写 patches，不重写报告。"}
