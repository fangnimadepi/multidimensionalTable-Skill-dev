"""Joint writing -> focused review -> editable reports and verified archive."""

import copy
import os
from pathlib import Path
import tempfile
import time
import uuid
from types import SimpleNamespace

from host_materials import catalog, digest
from host_content import render
from host_compose import sources, required_plans, normalize, validate, focus, check, patch_roots, apply_patches, canonical
from read_docx import file_hash, inside
from host_prompts import task, policy, schema, PACKAGE
from host_protocol import HostTaskPending, HostReportFailure
from review_common import atomic_write_json
from host_tasks import reading_task, submit_command, source_lines, command

VERSION = "host-report-v3"


def save_state(file, state):
    state["content_sha256"] = digest({key: value for key, value in state.items() if key != "content_sha256"})
    atomic_write_json(file, state)


def fingerprint():
    files = list((PACKAGE / "scripts").glob("host_*.py"))
    files += [PACKAGE / "references/review-report.md", PACKAGE / "references/achievement-reports.md",
              PACKAGE / "runtime/config/review_policy_v0.2.json"]
    files += [PACKAGE / "scripts" / name for name in ("read_docx.py", "local_reports.py")]
    files += [PACKAGE / "runtime/scripts" / name for name in ("project_report_contract.py", "project_report_presentation.py",
              "project_report_assurance.py", "project_report_synthesis.py", "project_report_findings.py", "project_report_profile.py", "pdf_ocr.py")]
    return digest({str(f.relative_to(PACKAGE)): file_hash(f) for f in sorted(files)})


def read_json(path):
    import json
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fail(folder, state, stage, errors):
    feedback = folder / "failed.json"
    atomic_write_json(feedback, {"stage": stage, "errors": errors})
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


def source_context(material, selected=None):
    return [{"id": key, "side": "任务书" if s["side"] == "plan" else "总结报告", "role": s["role"],
             "text": s["text"], **({"section": s["heading"]} if s.get("heading") else {}), **({"context": s["context"]} if s.get("context") else {})}
            for key, s in sources(material).items() if selected is None or key in selected]


def request(folder, state, stage, index, payload, attempt):
    body = task(stage, payload, state["types"])
    identifier = digest([VERSION, state["identity"], stage, index, attempt, body, state.get("joint")])
    prefix = str(len(state["accepted"]) + state["repairs"] + state.get("format_retries", 0) + 1)
    request_file = folder / ("request-" + prefix + ".json")
    response_file = folder / ("response-" + prefix + ".json")
    task_file = folder / ("task-" + prefix + ".md")
    template_file = folder / ("template-" + prefix + ".json")
    shape = body.pop("result_shape")
    envelope = {"request_id": identifier, **shape} if stage == "repair" else {"request_id": identifier, "result": shape}
    content = {"request_id": identifier, "stage": stage, "stage_part": index + 1, "revision_attempt": attempt,
               "task_file": str(task_file), "response_template": str(template_file), "response_file": str(response_file),
               "submit_command": submit_command(request_file, state["continuation"]["config_file"]),
               "continuation": state["continuation"], "response_envelope": envelope, **body}
    if os.name == "nt":
        import sys
        argv = [str(sys.executable).replace("\\", "/"), str(PACKAGE / "scripts/host_task.py").replace("\\", "/"),
                "--config", str(state["continuation"]["config_file"]).replace("\\", "/"), "submit", "--request", str(request_file).replace("\\", "/")]
        content["submit_bash"] = command(argv, windows=False)
    parts, current, size = [], [], 0
    for item in content.get("source_context", []):
        amount = len(item["text"]) + len(item.get("context", "")) + 100
        if current and size + amount > 24000:
            parts.append(current); current, size = [], 0
        current.append(item); size += amount
    if current:
        parts.append(current)
    extra = {}
    if len(parts) > 1:
        content["source_files"] = []
        for n, part in enumerate(parts, 1):
            path = folder / ("materials-" + prefix + "-" + str(n) + ".md")
            path.write_text("\n".join(source_lines(part)), encoding="utf-8")
            content["source_files"].append(str(path)); extra[str(path)] = file_hash(path)
    atomic_write_json(request_file, content)
    atomic_write_json(template_file, envelope)
    task_file.write_text(reading_task(content), encoding="utf-8")
    pending = {"request_id": identifier, "stage": stage, "stage_part": index + 1,
               "request_file": str(request_file), "request_sha256": digest(content),
               "response_file": str(response_file), "task_file": str(task_file), "response_template": str(template_file),
               "submit_command": content["submit_command"], "revision_attempt": attempt,
               "task_sha256": file_hash(task_file), "template_sha256": file_hash(template_file),
               "source_files": extra, "issued_at": time.time()}
    if content.get("submit_bash"):
        pending["submit_bash"] = content["submit_bash"]
    state.update(pending=pending, pending_polls=0)
    save_state(folder / "state.json", state)
    return pending


def response(folder, state, supplied):
    pending = state["pending"]
    path = inside(Path(pending["request_file"]), folder)
    if digest(read_json(path)) != pending["request_sha256"]:
        raise ValueError("待执行模型任务与已保存版本不一致。")
    for key, checksum in (("task_file", "task_sha256"), ("response_template", "template_sha256")):
        if file_hash(inside(Path(pending[key]), folder)) != pending[checksum]:
            raise ValueError("只可填写响应文件，任务说明或响应骨架发生了变化。")
    for file, checksum in pending.get("source_files", {}).items():
        if file_hash(inside(Path(file), folder)) != checksum:
            raise ValueError("待办原材料分段发生变化。")
    expected = inside(Path(pending["response_file"]), folder)
    if supplied is not None and inside(Path(supplied), folder) != expected:
        raise ValueError("响应文件不属于当前待办。")
    if not expected.is_file():
        if supplied is not None:
            return None, ["response:响应文件尚未创建，先填写再提交，不要轮询"]
        state["pending_polls"] = state.get("pending_polls", 0) + 1
        if state["pending_polls"] > 3:
            fail(folder, state, "no_progress", ["同一待办没有响应却反复续跑，已停止轮询"])
        save_state(folder / "state.json", state)
        raise HostTaskPending(copy.deepcopy(pending))
    if expected.stat().st_size > 2 * 1024**2:
        return None, ["response:响应超过大小限制"]
    try:
        raw = read_json(expected)
    except (ValueError, OSError):
        return None, ["response:不是可读取的 UTF-8 JSON"]
    if not isinstance(raw, dict) or raw.get("request_id") != pending["request_id"]:
        return None, ["response:需要匹配本次 request_id"]
    if pending["stage"] == "repair":
        return raw.get("patches"), []
    if not isinstance(raw.get("result"), dict):
        return None, ["response:需在 result 中返回当前阶段对象"]
    return raw["result"], []


def check_chunks(joint, material, types):
    import json
    lookup, groups, current = sources(material), focus(joint, material, types), []
    for group in groups:
        candidate = current + [group]
        selected = {k for item in candidate for k in item["sources"]}
        size = len(json.dumps(candidate, ensure_ascii=False)) + sum(len(lookup[k]["text"]) for k in selected)
        if current and size > 32000:
            yield current; current = []
        current.append(group)
    if current:
        yield current


def at(value, pointer):
    try:
        for part in pointer.strip("/").split("/"):
            value = value[int(part)] if isinstance(value, list) and part.isdigit() else value[part]
        return value
    except (KeyError, IndexError, TypeError):
        return None


def repair_payload(state, material):
    roots = patch_roots(state["feedback"])
    # Give a missing field its own shape, never another full-draft skeleton.
    shape = schema(state["types"])
    fields = []
    for path in roots:
        value = at(state["joint"], path)
        field = {"path": path, "value": value}
        if value is None:
            field["example"] = at(shape, path)
        fields.append(field)
    selected = set()
    def collect(value):
        if isinstance(value, dict):
            ids = value.get("sources", [])
            if isinstance(ids, list):
                selected.update(k for k in ids if isinstance(k, str))
            for v in value.values(): collect(v)
        elif isinstance(value, list):
            for v in value: collect(v)
    collect(fields)
    lookup = sources(material)
    # Repairs for missing modules, missing coverage or wrong numeric sources
    # need the relevant original text, not just a truncated source synopsis.
    needs_plan = "/plans" in roots
    needs_report = any(path in {"/review", "/achievement", "/forecast", "/key_facts"} for path in roots)
    if not selected or selected - lookup.keys():
        needs_plan = needs_report = True
    selected.update(k for k,s in lookup.items() if needs_plan and s["side"] == "plan" or needs_report and s["side"] == "report")
    return {"revision_feedback": state["feedback"], "format_feedback": state.get("format_feedback", []),
            "allowed_roots": roots, "fields": fields,
            "available_sources": [{"id": k, "role": s["role"], "summary": s["text"][:100]} for k,s in lookup.items()],
            "source_context": source_context(material, selected)}


def export(folder, state, args, run_dir, paths, facts, material, draft, claims, *, reexport_from=None):
    from word_reports import render_docx
    from feishu_report_bundle import REPORT_TITLES

    clock = time.perf_counter()
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
        checks = {kind: {"passed": True, "method": "focused_source_checks_and_host_self_review"} for kind in state["types"]}
        if len(state["types"]) == 2:
            checks["paired"] = {"passed": True, "method": "host_cross_report_self_review"}
        timing = {**state.get("timing", {}), "document_export_seconds": round(time.perf_counter() - clock, 4)}
        receipt = {"status": "checked", "record_id": args.record_id, "project_id": state["project_id"],
                   "material_hashes": state["hashes"], "checks": checks, "documents": documents,
                   "execution_mode": "host", "model": "WorkBuddy 当前会话模型", "provider": "host",
                   "review_method": "程序来源检查与宿主模型自查；非独立模型复核",
                   "direct_api_calls": 0, "host_steps_completed": len(state["accepted"]),
                   "timing": timing, "critical_items": len(focus(state["joint"], material, state["types"])),
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
    run_dir = Path(run_dir).resolve(strict=True)
    types = args.types
    if (not types or len(types) != len(set(types)) or set(types) - {"review", "achievement", "forecast"}
            or "forecast" in types and types != ["forecast"]):
        raise ValueError("报告类型重复或组合无效。")
    if (types == ["forecast"]) == bool(args.report):
        raise ValueError("任务书版只使用任务书；阶段报告需要配对总结。")
    paths = {"plan": source_path(run_dir, args.taskbook, "taskbook", config)}
    if args.report:
        paths["report"] = source_path(run_dir, args.report, "report", config)
    if sum(p.stat().st_size for p in paths.values()) > config.get("max_project_mib", 192) * 1024**2:
        raise ValueError("项目材料合计超过大小限制。")
    hashes = {side: file_hash(path) for side, path in paths.items()}
    continuation = getattr(args, "continuation", None) or {
        "kind": "local", "config_file": str(Path(os.environ.get("JINGYAN_CONFIG", run_dir.parent / "config.json")).resolve()),
        "run_dir": str(run_dir), "taskbook": args.taskbook, "report": args.report, "types": types,
        "record_id": args.record_id, "project_name": args.project_name}
    identity = digest([VERSION, str(run_dir), hashes, types, args.record_id, args.project_name, continuation,
                       config.get("review_scope", {}), fingerprint()])
    folder = inside(run_dir / "_host" / identity[:24], run_dir)
    with exclusive_work(folder):
        file = folder / "state.json"
        if file.exists():
            state = read_json(file)
            if state.get("identity") != identity or state.get("hashes") != hashes or state.get("content_sha256") != digest({k:v for k,v in state.items() if k != "content_sha256"}):
                raise ValueError("宿主任务记录与本次材料不一致。")
            for item in state["accepted"]:
                if file_hash(inside(Path(item["file"]), folder)) != item["sha256"]:
                    raise ValueError("已检查的阶段结果发生变化。")
        else:
            started = time.time(); clock = time.perf_counter()
            material, pid = create_catalog(args, run_dir, paths, config)
            atomic_write_json(folder / "materials.json", material)
            state = {"version": VERSION, "identity": identity, "hashes": hashes, "types": types,
                     "workflow_fingerprint": fingerprint(), "project_name": args.project_name, "record_id": args.record_id,
                     "project_id": pid, "started_at": started, "status": "working", "stage": "compose", "part": 0,
                     "attempt": 0, "repairs": 0, "format_retries": 0, "audits": [], "accepted": [],
                     "continuation": continuation, "materials_sha256": digest(material),
                     "timing": {"material_seconds": round(time.perf_counter()-clock, 4), "host_wait_seconds": 0.0, "core_validation_seconds": 0.0}}
            save_state(file, state)
        material = read_json(folder / "materials.json")
        if digest(material) != state["materials_sha256"]:
            raise ValueError("解析材料底稿发生变化。")
        if state["status"] == "failed":
            raise HostReportFailure(folder, state["failed_stage"], folder / "failed.json")
        if state["status"] == "checked":
            receipt = read_json(inside(Path(state["receipt"]), run_dir / "reports"))
            if receipt["material_hashes"] != hashes or any(file_hash(Path(d["docx"])) != d["sha256"] for d in receipt["documents"].values()):
                raise ValueError("已有报告文件与回执不一致。")
            return receipt
        supplied = getattr(args, "response", None)
        while True:
            stage, index = state["stage"], state["part"]
            payload = {"project_name": args.project_name, "review_scope": {k:material[k] for k in ("review_stage", "checkpoint_date")}}
            if stage == "compose":
                payload.update(source_context=source_context(material), required_plan_sources=required_plans(material))
            elif stage == "repair":
                payload.update(repair_payload(state, material))
            else:
                chunks = list(check_chunks(state["joint"], material, types)); groups = chunks[index]
                selected = {k for item in groups for k in item["sources"]}
                view = {k:{name:state["joint"][k][name] for name in (("overview", "conclusion", "pi_questions") if k == "review" else ("fact_sheet", "next_steps", "card"))} for k in types}
                payload.update(groups=groups, report_view=view, source_context=source_context(material, selected), total_parts=len(chunks))
            if state.get("feedback") and stage != "repair": payload["revision_feedback"] = state["feedback"]
            if not state.get("pending"): request(folder, state, stage, index, payload, state["attempt"])
            raw, errors = response(folder, state, supplied); supplied = None
            pending = state["pending"]
            clock = time.perf_counter(); candidate = raw
            if not errors:
                if stage == "repair":
                    try: candidate = normalize(apply_patches(state["joint"], raw, patch_roots(state["feedback"])))
                    except ValueError as error: errors = ["repair:" + str(error)]
                    if not errors and digest(candidate) == digest(state["joint"]): fail(folder,state,"repair",["成稿未按反馈修改，不能仅重新自查"])
                elif stage == "compose": candidate = normalize(raw)
                if not errors: errors = check(raw, groups) if stage == "check" else validate(candidate, material, types)
            state["timing"]["core_validation_seconds"] += round(time.perf_counter()-clock, 6)
            state["timing"]["host_wait_seconds"] += round(max(0, time.time()-pending["issued_at"]), 4)
            if errors:
                atomic_write_json(folder / ("feedback-" + pending["request_id"][:16] + ".json"), {"stage":stage,"errors":errors,"response":raw})
                if stage == "check" and all(e.startswith("check") for e in errors) or any(e.startswith(("response:", "repair:")) for e in errors):
                    if state["format_retries"] >= 1: fail(folder,state,stage,errors)
                    state["format_retries"] += 1
                    state.update(pending=None, attempt=state["attempt"]+1)
                    # A malformed patch does not erase the semantic feedback.
                    if stage != "repair": state["feedback"] = errors
                    else: state["format_feedback"] = errors
                else:
                    if state["repairs"] >= 2 or not patch_roots(errors): fail(folder,state,stage,errors)
                    if stage != "check": state["joint"] = candidate
                    state.update(pending=None, stage="repair", part=0, repairs=state["repairs"]+1,
                                 attempt=state["repairs"]+1, feedback=errors, audits=[])
                save_state(file,state); continue
            accepted = folder / ("accepted-" + pending["request_id"][:16] + ".json")
            atomic_write_json(accepted,{"request_id":pending["request_id"],"result":raw})
            state["accepted"].append({"file":str(accepted),"sha256":file_hash(accepted),"stage":stage})
            state.update(pending=None, attempt=0); state.pop("feedback",None); state.pop("format_feedback",None)
            if stage in {"compose", "repair"}:
                state.update(joint=candidate, stage="check", part=0, audits=[])
            else:
                state["audits"].append({"request_id":pending["request_id"],"result":raw});state["part"]+=1
                if state["part"] == len(list(check_chunks(state["joint"], material, types))):
                    draft, facts, claims = canonical(state["joint"],material,types)
                    return export(folder,state,args,run_dir,paths,facts,material,draft,claims)
            save_state(file,state)


def reexport(previous, run_dir, paths):
    folder = inside(Path(previous["diagnostics"]), run_dir / "_host")
    state = read_json(folder / "state.json")
    if state.get("content_sha256") != digest({k:v for k,v in state.items() if k != "content_sha256"}) or state.get("status") != "checked" or state.get("workflow_fingerprint") != fingerprint() or state.get("hashes") != previous["material_hashes"]:
        raise ValueError("宿主底稿的材料、规则或检查状态已变化，不能仅重新排版。")
    for item in state["accepted"]:
        if file_hash(inside(Path(item["file"]),folder))!=item["sha256"]: raise ValueError("已核对底稿发生变化。")
    material=read_json(folder/"materials.json")
    if digest(material)!=state["materials_sha256"] or validate(state["joint"],material,state["types"]): raise ValueError("底稿或当前规则不再接受旧稿。")
    draft,facts,claims=canonical(state["joint"],material,state["types"])
    args=SimpleNamespace(project_name=state["project_name"],record_id=state["record_id"])
    return export(folder,state,args,run_dir,paths,facts,material,draft,claims,reexport_from=previous["receipt"])
