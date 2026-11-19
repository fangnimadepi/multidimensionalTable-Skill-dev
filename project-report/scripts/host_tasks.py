"""Host-facing task files and commands; no model requests or credentials."""

import copy
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


def compact_facts(facts):
    result = copy.deepcopy(facts)
    for item in result["plan_items"] + result["facts"]:
        item["citations"] = list(dict.fromkeys(c["source_id"] for c in item["citations"]))
    return result


def compact_claims(claims):
    return [{key: value for key, value in claim.items() if key != "sources"} |
            {"source_ids": list(dict.fromkeys(c["source_id"] for c in claim["sources"]))} for claim in claims]


def skeleton(stage, payload, types):
    """Real IDs and empty fields, never pre-approved findings or audit results."""
    if stage == "facts":
        return {"plan_items": [], "facts": [], "coverage": [
            {"source_id": s["source_id"], "disposition": "", "reason": ""}
            for s in payload["sources"] if s["plan_candidate"]]}
    if stage == "check":
        return {"checks": [{"claim_id": c["claim_id"], "supported": None, "reason": ""} for c in payload["claims"]],
                "plan_coverage_ok": None, "stage_scope_ok": None, "cross_report_consistent": None,
                "readable_and_complete": None, "issues": [], "editorial_notes": []}

    def unit(kind="分析"):
        return {"text": "", "kind": kind, "plan_ids": [], "fact_ids": []}

    result = {}
    if "review" in types:
        result["review"] = {"overview": [unit()], "plan_summary": [unit("计划")],
                            "progress_summary": [unit("事实")], "comparisons": [
            {"item_id": item["item_id"], "fact_ids": [],
             "status": {"总体": "总体参考", "后续": "后续节点"}.get(item["scope"], ""), "progress": "", "judgement": ""}
            for item in payload["facts"]["plan_items"]],
            "findings": [], "conclusion": [unit()], "pi_questions": [], "manager_notes": []}
    for kind in types:
        if kind == "review":
            continue
        result[kind] = {"value_title": unit(), "fact_sheet": [
            {"label": label, "content": unit()} for label in ("解决什么问题", "形成什么", "有什么用", "当前阶段")],
            "short_version": [unit(), unit()], "long_intro": [unit()],
            "long_sections": [{"title": unit(), "paragraphs": [unit()]} for _ in range(3)],
            "next_steps": [unit("建议")], "card": {k: unit() for k in ("delivered", "method", "value", "stage", "next")}}
        if kind == "forecast":
            result[kind]["overview"] = [unit("计划")]
    return result


def reading_task(content):
    """Readable material once, rather than JSON-escaped text plus code study."""
    lines = ["# 鲸研当前待办", "", "阶段：" + content["stage"] + "；分块：" + str(content["stage_part"]) +
             "/" + str(content.get("total_parts", 1)), "修订次数：" + str(content["revision_attempt"]) + "/1", "",
             "只完成当前待办：填写响应文件，然后执行下方提交命令。不要轮询，不要编写校验或生成脚本。", "",
             "响应骨架：" + content["response_template"], "写入目标：" + content["response_file"], "",
             "提交命令（同一命令完成校验、提交与续跑）：", "", "```", content["submit_command"], "```", "",
             "## 本次要求", content["instructions"], "", "## 判定规则"]
    lines += ["- " + rule for rule in content["business_rules"]]
    lines += ["", "## 判定示例"]
    for example in content["calibration_examples"]:
        lines += ["- " + example["情形"] + " 应采用：" + example["采用"] + " 避免：" + example["避免"]]
    lines += ["", "## 数据", "项目：" + str(content.get("project_name") or ""),
              "审查范围：" + json.dumps(content.get("review_scope", {}), ensure_ascii=False)]
    if content.get("revision_feedback"):
        lines += ["", "## 本次修订反馈", json.dumps(content["revision_feedback"], ensure_ascii=False)]
        # Previous results remain in a dedicated file; keep the reading task
        # short, and use that file itself as the repair template.
    if content["stage"] == "facts":
        for source in content["sources"]:
            lines += ["", "### " + source["source_id"] + " | " + source["side"] + " | " + source["role"] +
                      (" | 计划候选，需填写 coverage" if source["plan_candidate"] else ""), source["text"]]
            if source.get("context"):
                lines += ["表格语境：" + source["context"]]
    else:
        for key in ("facts", "draft", "claims", "source_context"):
            if key in content:
                lines += ["", "## " + key, json.dumps(content[key], ensure_ascii=False, separators=(",", ":"))]
    if content["stage"] == "draft":
        lines += ["", "## 报告结构", json.dumps(content["response_envelope"]["result"], ensure_ascii=False)]
    for name, guide in content.get("writing_guides", {}).items():
        lines += ["", "## " + name, guide]
    return "\n".join(lines) + "\n"


def pending_result(model_task, resume_arguments=None):
    return {"status": "needs_model", "execution_mode": "host", "model_task": model_task,
            **({"resume_arguments": resume_arguments} if resume_arguments else {}),
            "next_command": model_task["submit_command"],
            "next_action": "读取 task_file 和 response_template，用文件写入工具填写 response_file，然后只执行 next_command。不要自写脚本或轮询。"}
