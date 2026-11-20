"""Resumable host-model report workflow. This module never calls a model API."""

import copy
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from types import SimpleNamespace

from host_materials import catalog, batches, digest, compact_source
from read_docx import file_hash, inside
from host_content import validate_facts, merge_facts, validate_draft, validate_check, render, normalize_draft
from host_prompts import task, policy, PACKAGE
from host_protocol import HostTaskPending, HostReportFailure
from review_common import atomic_write_json
from host_tasks import compact_facts, compact_claims, skeleton, reading_task, submit_command, repair_details

VERSION = "host-report-v2"


def save_state(file, state):
    state["content_sha256"] = digest({key: value for key, value in state.items() if key != "content_sha256"})
    atomic_write_json(file, state)


def fingerprint():
    files = list((PACKAGE / "scripts").glob("host_*.py"))
    files += [PACKAGE / "references/review-report.md",
              PACKAGE / "references/achievement-reports.md", PACKAGE / "runtime/config/review_policy_v0.2.json"]
    files += [PACKAGE / "scripts" / name for name in ("read_docx.py", "local_reports.py")]
    files += [PACKAGE / "runtime/scripts" / name for name in (
        "project_report_contract.py", "project_report_presentation.py", "project_report_assurance.py",
        "project_report_synthesis.py", "project_report_findings.py", "project_report_profile.py", "pdf_ocr.py")]
    return digest({str(p.relative_to(PACKAGE)): file_hash(p) for p in sorted(files)})


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fail(folder, state, stage, errors, details=None):
    feedback = folder / "failed.json"
    atomic_write_json(feedback, {"stage": stage, "errors": errors, "items": details or []})
    state.update(status="failed", failed_stage=stage)
    save_state(folder / "state.json", state)
    raise HostReportFailure(folder, stage, feedback)


def create_catalog(args, run_dir, paths, config):
    from local_reports import read_material
    from build_contract_review import PROJECT_ID_PATTERN
    from project_report_profile import cover_lines, extract

    documents = {}
    for side, path in paths.items():
        result = read_material(run_dir, path, "taskbook" if side == "plan" else "report")
        documents[side] = read_json(Path(result["json_path"]))
        if result.get("text_extraction") != "succeeded":
            raise ValueError("输入材料没有足够可读文字，请补充可解析文件。")
    ids = {match.group() for _, line in cover_lines(paths["plan"]) for match in PROJECT_ID_PATTERN.finditer(line)}
    pid = next(iter(ids)) if len(ids) == 1 else None
    profile = extract(paths["plan"], pid, run_dir)
    scope = config.get("review_scope", {})
    rules = policy()
    material = catalog(documents, profile, scope.get("checkpoint_date", rules["checkpoint_date"]),
                       scope.get("review_stage", rules["review_stage"]))
    if "report" in paths and not any(source["side"] == "report" for source in material["sources"]):
        raise ValueError("总结报告没有可读正文，不能当作缺材料的任务书版处理。")
    return material, pid or "LOCAL-" + file_hash(paths["plan"])[:16]


def request(folder, state, stage, index, payload, attempt):
    body = task(stage, payload, state["types"])
    request_id = digest([VERSION, state["identity"], stage, index, attempt, body])
    request_file = folder / ("request-" + request_id[:16] + ".json")
    response_file = folder / ("response-" + request_id[:16] + ".json")
    task_file = folder / ("task-" + request_id[:16] + ".md")
    template_file = folder / ("template-" + request_id[:16] + ".json")
    content = {"request_id": request_id, "stage": stage, "stage_part": index + 1,
               "revision_attempt": attempt, "task_file": str(task_file), "response_template": str(template_file),
               "submit_command": submit_command(request_file, state["continuation"]["config_file"]),
               "continuation": state["continuation"],
               "response_file": str(response_file), "response_envelope": {"request_id": request_id, "result": body.pop("result_shape")},
               **body}
    pending = {"request_id": request_id, "stage": stage, "stage_part": index + 1,
               "request_file": str(request_file), "response_file": str(response_file),
               "request_sha256": digest(content), "task_file": str(task_file), "response_template": str(template_file),
               "submit_command": content["submit_command"], "total_parts": content.get("total_parts", 1),
               "revision_attempt": attempt}
    if request_file.exists() and digest(read_json(request_file)) != pending["request_sha256"]:
        raise ValueError("本次模型任务文件发生变化，请联系维护人员。")
    atomic_write_json(request_file, content)
    result = state.get("previous_result")
    atomic_write_json(template_file, {"request_id": request_id, "result": result if isinstance(result, dict) else skeleton(stage, payload, state["types"])})
    task_file.write_text(reading_task(content), encoding="utf-8")
    pending.update(task_sha256=file_hash(task_file), template_sha256=file_hash(template_file))
    state["pending"] = pending
    state["pending_polls"] = 0
    save_state(folder / "state.json", state)
    return pending


def response(folder, state, supplied):
    pending = state["pending"]
    request_file = inside(Path(pending["request_file"]), folder)
    if digest(read_json(request_file)) != pending["request_sha256"]:
        raise ValueError("待执行模型任务与已保存版本不一致。")
    for key, checksum in (("task_file", "task_sha256"), ("response_template", "template_sha256")):
        if file_hash(inside(Path(pending[key]), folder)) != pending[checksum]:
            raise ValueError("只可填写响应文件，任务说明或响应骨架发生了变化。")
    expected = inside(Path(pending["response_file"]), folder)
    if supplied is not None and inside(Path(supplied), folder) != expected:
        raise ValueError("响应文件不属于当前待办，请使用 model_task 指定的 response_file。")
    if not expected.is_file():
        if supplied is not None:
            return None, ["响应文件尚未创建；先用文件写入工具写到 response_file，再提交。不要轮询或研究源码。"]
        state["pending_polls"] = state.get("pending_polls", 0) + 1
        if state["pending_polls"] > 3:
            fail(folder, state, "no_progress", ["同一待办重复续跑但没有响应文件；已停止轮询。请先完成待办，勿反复运行命令。"])
        save_state(folder / "state.json", state)
        raise HostTaskPending(copy.deepcopy(pending))
    if expected.stat().st_size > 2 * 1024**2:
        return None, ["响应超过大小限制，请仅返回指定阶段的 JSON 结果"]
    try:
        raw = read_json(expected)
    except (OSError, ValueError):
        return None, ["响应不是可读取的 UTF-8 JSON，请修复语法，不修改原材料"]
    if not isinstance(raw, dict) or raw.get("request_id") != pending["request_id"] or not isinstance(raw.get("result"), dict):
        return None, ["响应需要匹配 request_id，并在 result 中返回当前阶段对象"]
    return raw["result"], []


def audit_chunks(claims, material, draft):
    lookup = {s["source_id"]: s for s in material["sources"]}
    current = []
    for claim in claims:
        candidate = current + [claim]
        selected = {c["source_id"] for value in candidate for c in value["sources"]}
        size = len(json.dumps([compact_claims(candidate), [compact_source(lookup[k]) for k in selected]], ensure_ascii=False))
        if current and size > 48000:
            yield current
            current = []
        current.append(claim)
    if current:
        yield current


def export(folder, state, args, run_dir, paths, facts, material, draft, claims, *, reexport_from=None):
    from word_reports import render_docx
    from feishu_report_bundle import REPORT_TITLES

    parent = inside(run_dir / "reports", run_dir)
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / ("reexport-" + uuid.uuid4().hex[:12] if reexport_from else "attempt-host-" + state["identity"][:16])
    if output.exists():
        raise ValueError("导出目录已有文件但缺少完成回执，请联系维护人员核对。")
    title = args.project_name or paths["plan"].stem
    with tempfile.TemporaryDirectory(prefix=".host-export-", dir=parent) as temp:
        staging = Path(temp) / output.name
        staging.mkdir()
        documents = {}
        for kind in state["types"]:
            markdown, card = render(kind, draft, facts, material, title, paths, claims)
            md = staging / (REPORT_TITLES[kind] + ".md")
            md.write_text(markdown, encoding="utf-8")
            word = md.with_suffix(".docx")
            stats = render_docx(markdown, word, REPORT_TITLES[kind], card=card)
            documents[kind] = {"docx": str(output / word.name), "markdown": str(output / md.name),
                               "bytes": stats["bytes"], "sha256": stats["sha256"],
                               "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               "internal_links": 0, "bookmarks": 0, "card": stats["card"]}
        if any(file_hash(path) != state["hashes"][side] for side, path in paths.items()):
            raise ValueError("材料在分析期间发生变化，已停止导出。")
        checks = {kind: {"passed": True, "method": "source_checks_and_host_self_review"} for kind in state["types"]}
        if len(state["types"]) == 2:
            checks["paired"] = {"passed": True, "method": "host_cross_report_self_review"}
        receipt = {"status": "checked", "record_id": args.record_id, "project_id": state["project_id"],
                   "material_hashes": state["hashes"], "checks": checks, "documents": documents,
                   "execution_mode": "host", "model": "WorkBuddy 当前会话模型", "provider": "host",
                   "review_method": "程序来源检查与宿主模型自查；非独立模型复核",
                   "direct_api_calls": 0, "host_steps_completed": len(state["accepted"]),
                   "seconds": round(time.time() - state["started_at"], 2),
                   "diagnostics": str(folder), "receipt": str(output / "checked.json")}
        if reexport_from:
            receipt.update(source_receipt=reexport_from, export_only=True, host_steps_completed=0)
        atomic_write_json(staging / "checked.json", receipt)
        staging.rename(output)
    if not reexport_from:
        state.update(status="checked", receipt=receipt["receipt"])
        save_state(folder / "state.json", state)
    return receipt


def advance(args, root, run_dir, config):
    from local_reports import source_path, exclusive_work
    from material_io import validate_document

    run_dir = Path(run_dir).resolve(strict=True)
    types = args.types
    if (not types or len(types) != len(set(types)) or set(types) - {"review", "achievement", "forecast"}
            or "forecast" in types and types != ["forecast"]):
        raise ValueError("报告类型重复或组合无效。")
    if (types == ["forecast"]) == bool(args.report):
        raise ValueError("任务书版只使用任务书；阶段报告需要配对的总结材料。")
    paths = {"plan": source_path(run_dir, args.taskbook, "taskbook", config)}
    if args.report:
        paths["report"] = source_path(run_dir, args.report, "report", config)
    if sum(p.stat().st_size for p in paths.values()) > config.get("max_project_mib", 192) * 1024**2:
        raise ValueError("项目材料合计超过大小限制。")
    for path in paths.values():
        validate_document(path, path.suffix.lower())
    hashes = {side: file_hash(path) for side, path in paths.items()}
    continuation = getattr(args, "continuation", None) or {
        "kind": "local", "config_file": str(Path(os.environ.get("JINGYAN_CONFIG", run_dir.parent / "config.json")).resolve()),
        "run_dir": str(run_dir), "taskbook": args.taskbook, "report": args.report,
        "types": types, "record_id": args.record_id, "project_name": args.project_name}
    identity = digest([VERSION, str(run_dir), hashes, types, args.record_id, args.project_name, continuation,
                       config.get("review_scope", {}), fingerprint()])
    folder = inside(run_dir / "_host" / identity[:24], run_dir)
    with exclusive_work(folder):
        file = folder / "state.json"
        if file.exists():
            state = read_json(file)
            if (state.get("identity") != identity or state.get("hashes") != hashes
                    or state.get("content_sha256") != digest({k: v for k, v in state.items() if k != "content_sha256"})):
                raise ValueError("宿主任务记录与本次材料不一致。")
            for accepted in state["accepted"]:
                if file_hash(inside(Path(accepted["file"]), folder)) != accepted["sha256"]:
                    raise ValueError("已检查的阶段结果发生变化，不能继续沿用原结论。")
        else:
            material, pid = create_catalog(args, run_dir, paths, config)
            atomic_write_json(folder / "materials.json", material)
            state = {"version": VERSION, "identity": identity, "hashes": hashes, "types": types,
                     "workflow_fingerprint": fingerprint(), "project_name": args.project_name, "record_id": args.record_id,
                     "project_id": pid, "started_at": time.time(), "status": "working", "stage": "facts",
                     "part": 0, "attempt": 0, "draft_attempt": 0, "facts": [], "audits": [], "accepted": []}
            state["continuation"] = continuation
            state["materials_sha256"] = digest(material)
            save_state(file, state)

        material = read_json(folder / "materials.json")
        if digest(material) != state["materials_sha256"]:
            raise ValueError("解析材料底稿发生变化，不能继续使用原待办。")
        if state["status"] == "failed":
            raise HostReportFailure(folder, state["failed_stage"], folder / "failed.json")
        if state["status"] == "checked":
            receipt = read_json(inside(Path(state["receipt"]), run_dir / "reports"))
            if receipt["material_hashes"] != hashes or any(file_hash(Path(doc["docx"])) != doc["sha256"] for doc in receipt["documents"].values()):
                raise ValueError("已有报告文件与回执不一致。")
            return receipt
        source_chunks = list(batches(material["sources"]))
        supplied = getattr(args, "response", None)
        while True:
            stage, index = state["stage"], state["part"]
            if stage == "facts":
                payload = {"project_name": args.project_name, "review_scope": {key: material[key] for key in ("review_stage", "checkpoint_date")},
                           "sources": [compact_source(s) for s in source_chunks[index]], "total_parts": len(source_chunks)}
            else:
                facts = merge_facts(state["facts"])
                if not facts["plan_items"]:
                    fail(folder, state, "facts", ["任务书未提取出可用于报告的计划条件，不能按零差异交付。"])
                if types != ["forecast"] and not any(f["kind"] == "阶段进展" for f in facts["facts"]):
                    fail(folder, state, "facts", ["总结报告未提取出当前进展，请检查取材完整性。"])
                payload = {"project_name": args.project_name, "facts": compact_facts(facts),
                           "review_scope": {key: material[key] for key in ("review_stage", "checkpoint_date")}}
                if stage == "check":
                    claims, errors = validate_draft(state["draft"], facts, material, types)
                    if errors:
                        fail(folder, state, "draft_integrity", errors)
                    audit_parts = list(audit_chunks(claims, material, state["draft"]))
                    chunk = audit_parts[index]
                    selected = {c["source_id"] for claim in chunk for c in claim["sources"]}
                    payload.update(draft=state["draft"], claims=compact_claims(chunk), total_parts=len(audit_parts),
                                   source_context=[compact_source(s) for s in material["sources"] if s["source_id"] in selected])
            if state.get("feedback"):
                payload["revision_feedback"] = state["feedback"]
                payload["revision_feedback_details"] = state.get("feedback_details", [])
            if state.get("previous_result") is not None:
                payload["previous_result"] = state["previous_result"]
            if not state.get("pending"):
                request(folder, state, stage, index, payload, state["attempt"])
            raw, errors = response(folder, state, supplied)
            supplied = None
            pending = state["pending"]
            checked = None
            if not errors:
                if stage == "facts":
                    checked, errors = validate_facts(raw, source_chunks[index])
                elif stage == "draft":
                    raw, normalization_errors = normalize_draft(raw, facts)
                    _, errors = validate_draft(raw, facts, material, types)
                    errors = normalization_errors + errors
                    if state.get("rejected_draft_digest") == digest(raw):
                        errors.append("成稿未按明确的不支持意见修订，不能仅重问自查")
                else:
                    errors = validate_check(raw, chunk, len(types) == 2)
            if errors:
                details = repair_details(errors, raw, facts if stage != "facts" else None)
                feedback = folder / ("feedback-" + pending["request_id"][:16] + ".json")
                atomic_write_json(feedback, {"stage": stage, "errors": errors, "items": details, "response": raw})
                rows = raw.get("checks") if isinstance(raw, dict) else None
                negative = stage == "check" and isinstance(raw, dict) and (
                    any(isinstance(r, dict) and r.get("supported") is False for r in (rows if isinstance(rows, list) else []))
                    or any(raw.get(k) is False for k in ("plan_coverage_ok", "stage_scope_ok", "cross_report_consistent"))
                    or bool(raw.get("issues")))
                if negative:
                    if state["draft_attempt"] >= 1:
                        fail(folder, state, stage, errors, details)
                    state.update(stage="draft", part=0, attempt=1, draft_attempt=1,
                                 previous_result=state["draft"], rejected_draft_digest=digest(state["draft"]), audits=[])
                elif state["attempt"] >= 1:
                    fail(folder, state, stage, errors, details)
                else:
                    state["attempt"] += 1
                    if stage == "draft":
                        state["draft_attempt"] = state["attempt"]
                    state["previous_result"] = raw
                state.update(pending=None, feedback=errors, feedback_details=details)
                save_state(file, state)
                continue
            accepted = folder / ("accepted-" + pending["request_id"][:16] + ".json")
            atomic_write_json(accepted, {"request_id": pending["request_id"], "result": raw})
            state["accepted"].append({"file": str(accepted), "sha256": file_hash(accepted), "stage": stage})
            state.update(pending=None, attempt=0)
            state.pop("feedback", None)
            state.pop("feedback_details", None)
            state.pop("previous_result", None)
            if stage == "facts":
                state["facts"].append(checked)
                state["part"] += 1
                if state["part"] == len(source_chunks):
                    state.update(stage="draft", part=0)
            elif stage == "draft":
                state.update(draft=raw, stage="check", part=0)
            else:
                state["audits"].append({"request_id": pending["request_id"], "result": raw})
                state["part"] += 1
                if state["part"] == len(list(audit_chunks(claims, material, state["draft"]))):
                    return export(folder, state, args, run_dir, paths, facts, material, state["draft"], claims)
            save_state(file, state)

def reexport(previous, run_dir, paths):
    folder = inside(Path(previous["diagnostics"]), run_dir / "_host")
    state = read_json(folder / "state.json")
    expected = digest({key: value for key, value in state.items() if key != "content_sha256"})
    if (state.get("content_sha256") != expected or state.get("status") != "checked"
            or state.get("workflow_fingerprint") != fingerprint() or state.get("hashes") != previous["material_hashes"]):
        raise ValueError("宿主底稿的材料、规则或检查状态已变化，不能仅重新排版。")
    for accepted in state["accepted"]:
        if file_hash(inside(Path(accepted["file"]), folder)) != accepted["sha256"]:
            raise ValueError("已核对底稿发生变化，停止重新导出。")
    material = read_json(folder / "materials.json")
    if digest(material) != state["materials_sha256"]:
        raise ValueError("原材料解析底稿发生变化。")
    facts = merge_facts(state["facts"])
    claims, errors = validate_draft(state["draft"], facts, material, state["types"])
    if errors:
        raise ValueError("当前规则不再接受旧稿，请重新生成。")
    state["started_at"] = time.time()
    args = SimpleNamespace(project_name=state["project_name"], record_id=state["record_id"])
    return export(folder, state, args, run_dir, paths, facts, material, state["draft"], claims,
                  reexport_from=previous["receipt"])
