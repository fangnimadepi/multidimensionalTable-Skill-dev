"""Plan a Feishu report batch, then download, generate and verify its deliveries."""

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

from feishu_connector import Connector, ConnectorError, NeedsSelection, attachments, identifier, repeated
from local_reports import WorkerFailure, exclusive_work, generate, load_json, setup_runtime, workspace
from plan_batch import make_plan
from read_docx import file_hash, inside

sys.path.append(str(Path(__file__).resolve().parents[1] / "runtime/scripts"))
from feishu_report_bundle import REPORT_TITLES
from review_common import atomic_write_json, api_key


ALIASES = {
    "name": {"项目名", "项目名称", "课题名称", "课题名"},
    "taskbook": {"项目任务书", "任务书", "项目计划书", "课题任务书"},
    "report": {"项目中期报告", "中期报告", "项目阶段总结报告", "阶段总结报告", "项目总结报告",
               "中期总结报告", "项目中期总结报告", "结题报告", "结题总结报告"},
    "output": {"项目报告", "项目报告归档", "鲸研报告", "鲸研项目报告", "报告归档", "生成报告", "项目分析报告"},
}
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}
STANDARD_REPORT_TYPES = ("review", "achievement")


def field_mapping(data, args):
    selected = {}
    kinds = {"name": {"text"}, **dict.fromkeys(("taskbook", "report", "output"), {"attachment"})}
    generated = {
        item["id"] for item in data if item["type"] == "attachment"
        and (item["name"] in ALIASES["output"]
             or re.search(r"(?:鲸研|AI\s*生成|生成报告存放|生成的报告)", str(item.get("description", "")), re.I))
    }
    explicit = {}
    for role in ALIASES:
        value = getattr(args, role + "_column")
        if value:
            matches = [item for item in data if value in (item["id"], item["name"]) and item["type"] in kinds[role]]
            if len(matches) != 1:
                raise NeedsSelection(f"请核对指定的{role_label(role)}列。")
            explicit[role] = matches[0]["id"]
    if "output" in explicit and any(item["id"] == explicit["output"] and item["id"] not in generated
                                    and item["name"] in ALIASES["taskbook"] | ALIASES["report"] for item in data):
        raise NeedsSelection("指定的归档列名称对应项目输入材料，请另选报告归档列。")
    for role in ALIASES:
        if role == "output" and not args.archive:
            continue
        if role == "report" and (args.no_report or args.types == ["forecast"]):
            continue
        eligible = [item for item in data if item["type"] in kinds[role] and item["id"] not in explicit.values()]
        if role == "output":
            matches = [item for item in eligible if item["id"] in generated]
        else:
            matches = [item for item in eligible if item["id"] not in generated and item["name"] in ALIASES[role]]
        if role == "name" and not matches and len(eligible) == 1 and eligible[0]["name"] == "文本":
            matches = eligible
        if role in explicit:
            selected[role] = explicit[role]
        elif len(matches) == 1:
            selected[role] = matches[0]["id"]
        elif role == "output" and not matches:
            raise NeedsSelection("未找到明确的归档附件列，请指定已有列或确认新建「项目报告归档」。",
                                 [item["name"] for item in eligible])
        elif matches or role in {"name", "taskbook"}:
            raise NeedsSelection(f"请指定{role_label(role)}列。", [item["name"] for item in matches or eligible])
    if "report" not in selected and not args.no_report and args.types != ["forecast"]:
        unknown = [item["name"] for item in data if item["type"] == "attachment"
                   and item["id"] not in generated and item["id"] not in selected.values()]
        if unknown:
            raise NeedsSelection("请指出阶段总结列，或确认这张表没有阶段总结。", unknown)
    if len(set(selected.values())) != len(selected):
        raise ValueError("项目名称、输入和归档列须分别对应不同的字段。")
    return selected, {role: next(item["name"] for item in data if item["id"] == key) for role, key in selected.items()}


def role_label(role):
    return {"name": "项目名称", "taskbook": "任务书", "report": "阶段总结", "output": "归档"}[role]


def project_name(value):
    if isinstance(value, list):
        value = "".join(str(part.get("text", "")) for part in value if isinstance(part, dict))
    return str(value or "").strip()


def snapshot(row, fields):
    return {role: attachments(row.get(fields[role])) if role in fields else [] for role in ("taskbook", "report")}


def choose_projects(rows, args, record_link):
    if args.project:
        matches = []
        for project in args.project:
            found = [row for row in rows if project in (row["record_id"], row["project_name"])]
            if len(found) != 1:
                raise NeedsSelection(f"无法唯一定位项目「{project}」，请确认项目名称。")
            matches.append(found[0])
        rows = matches
    elif args.count is not None and len(rows) != args.count:
        raise NeedsSelection(f"当前范围有 {len(rows)} 个项目，请指定要处理的 {args.count} 个。")
    elif args.first is not None:
        if len(rows) < args.first:
            raise NeedsSelection(f"当前视图只找到 {len(rows)} 个项目。")
        rows = rows[:args.first]
    elif not (args.all or record_link or len(rows) == 1 or args.count is not None):
        raise NeedsSelection("请指定项目名单或确认处理当前范围内的全部项目。")
    if not rows:
        raise NeedsSelection("当前范围没有可识别的项目。")
    return rows


def progress(**event):
    print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)


def save_plan(path, plan):
    plan["counts"] = dict(Counter(entry["status"] for entry in plan["records"]))
    atomic_write_json(path, plan)


def summarize(plan, path):
    return {"batch": str(path), "table": plan["table_name"], "columns": plan["column_names"],
            "total": plan["total"], "counts": plan["counts"], "archive": plan["archive_requested"],
            "requested_types": plan["requested_types"], "preflight_seconds": plan.get("preflight_seconds"),
            "projects": [{"record_id": entry["record_id"], "name": entry["project_name"],
                          "status": entry["status"], "types": entry["types"], "reason": entry["reason"],
                          **{key: entry[key] for key in ("choices", "receipt", "diagnostics", "timing", "archive")
                             if entry.get(key)}} for entry in plan["records"]]}


def inspect(connector, root, args):
    started = time.monotonic()
    table = connector.resolve(args.target, args.table)
    fields, names = field_mapping(connector.fields(table), args)
    view = None if args.whole_table else table["view_id"]
    if args.first is not None and (not view or table.get("record_id")):
        raise NeedsSelection("处理前若干项需要指定有明确顺序的飞书视图链接。")
    rows = []
    for row in connector.records(table, list(fields.values()), view):
        sources = snapshot(row, fields)
        name = project_name(row[fields["name"]])
        if name or any(sources.values()):
            rows.append({"record_id": row["record_id"], "project_name": name, **sources, "source_cells": sources,
                         "existing_reports": attachments(row[fields["output"]]) if "output" in fields else []})
    selected = choose_projects(rows, args, table.get("record_id"))
    for record_id, role, token in args.material or []:
        match = next((row for row in selected if row["record_id"] == record_id), None)
        if match is None or role not in ("taskbook", "report"):
            raise ValueError("指定的材料不属于本次选定项目或材料角色。")
        files = [entry for entry in match[role] if entry["file_token"] == token]
        if len(files) != 1:
            raise ValueError("所选附件版本不在指定项目的材料列中。")
        match[role] = files
    plan = make_plan({**table, "fields": fields, "records": selected}, args.types, 5)
    batches = inside(root / "_batches", root)
    batches.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="feishu-", dir=batches))
    plan.update(version="feishu-batch-v1", table_name=table["table_name"], column_names=names, view_id=view,
                archive_requested=args.archive, replace=args.replace)
    for entry, row in zip(plan["records"], selected):
        entry["source_cells"] = row["source_cells"]
        if entry["status"] == "needs_selection":
            entry["choices"] = {role: entry[role] for role in ("taskbook", "report") if len(entry[role]) > 1}
    plan["preflight_seconds"] = round(time.monotonic() - started, 2)
    path = folder / "batch.json"
    save_plan(path, plan)
    return {"status": "planned", **summarize(plan, path)}


def current_record(connector, plan, entry):
    fields = plan["fields"]
    row = connector.record(plan, list(fields.values()), entry["record_id"])
    current = snapshot(row, fields)
    expected = entry["source_cells"]
    if any(sorted(current[role], key=lambda file: file["file_token"]) != sorted(expected[role], key=lambda file: file["file_token"])
           for role in expected if role == "taskbook" or plan["requested_types"] != ["forecast"]):
        raise ValueError("本项目的输入附件已变更，请重新清点。")
    if project_name(row[fields["name"]]) != entry["project_name"]:
        raise ValueError("项目名称已变更，请重新清点。")
    return attachments(row[fields["output"]]) if "output" in fields else []


def filename(name):
    name = re.split(r"[/\\]", name)[-1].strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).rstrip(". ")
    stem, suffix = Path(name).stem, Path(name).suffix
    if not stem or stem.split(".", 1)[0].upper() in WINDOWS_DEVICES:
        stem = "材料_" + stem
    return stem.encode("utf-8")[:220].decode("utf-8", errors="ignore") + suffix


def fetch_materials(connector, config, plan, entry, run_dir, stage, checkpoint):
    materials = {"taskbook": entry["taskbook"][0]}
    if entry["types"] != ["forecast"]:
        materials["report"] = entry["report"][0]
    if sum(file["size"] for file in materials.values()) > config.get("max_project_mib", 192) * 1024**2:
        raise ValueError("项目输入材料合计超过配置限制。")
    steps = entry["steps"].setdefault("materials", {})
    for role, attachment in materials.items():
        if attachment["size"] > config.get("max_file_mib", 128) * 1024**2:
            raise ValueError(f"附件超过配置限制：{attachment['name']}")
        relative = Path("raw", role, filename(attachment["name"]))
        target = inside(run_dir / relative, run_dir)
        prior = steps.get(role, {})
        if (prior.get("source") == attachment and target.is_file() and target.stat().st_size == attachment["size"]
                and file_hash(target) == prior.get("sha256")):
            continue
        temporary = stage / f"download-{role}{relative.suffix}"
        retries = min(1, max(0, int(config.get("download_retries", 1))))
        for attempt in range(retries + 1):
            try:
                digest = connector.download(plan, entry["record_id"], attachment, temporary)
                break
            except ConnectorError as error:
                if not error.retryable or attempt == retries:
                    raise
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(temporary, target)
        if file_hash(target) != digest:
            raise ValueError("复制输入附件后摘要不一致。")
        steps[role] = {"source": attachment, "path": relative.as_posix(), "sha256": digest}
        checkpoint()
    return steps


def validate_receipt(receipt, run_dir, entry, materials):
    hashes = {("plan" if role == "taskbook" else role): file["sha256"] for role, file in materials.items()}
    checks = set(entry["types"]) | ({"paired"} if len(entry["types"]) == 2 else set())
    documents = receipt["documents"]
    if (receipt.get("status") != "checked" or receipt.get("record_id") != entry["record_id"]
            or receipt.get("material_hashes") != hashes or set(documents) != set(entry["types"])
            or any(receipt.get("checks", {}).get(kind, {}).get("passed") is not True for kind in checks)):
        raise ValueError("报告回执无法绑定本次项目及已校验材料。")
    for document in documents.values():
        path = inside(Path(document["docx"]), run_dir / "reports")
        if not path.is_file() or path.suffix.lower() != ".docx" or path.stat().st_size != document["bytes"] or file_hash(path) != document["sha256"]:
            raise ValueError("本地 Word 报告与已校验回执不一致。")


def recover_receipt(run_dir, entry, materials):
    if entry.get("receipt"):
        path = inside(Path(entry["receipt"]), run_dir / "reports")
    else:
        paths = list((run_dir / "reports").glob("attempt-*/checked.json"))
        if len(paths) > 1:
            raise ValueError("本次运行目录存在多个回执，请核对报告版本。")
        if not paths:
            return None
        path = paths[0]
    receipt = load_json(path)
    validate_receipt(receipt, run_dir, entry, materials)
    return receipt


def prior_verified(run_dir, plan, entry):
    verified = {}
    for path in run_dir.parent.glob("*/archive.json"):
        if path.parent == run_dir:
            continue
        try:
            previous = load_json(path)
            if previous.get("record_id") != entry["record_id"] or previous.get("status") != "archived":
                continue
            if previous.get("field_id") == plan["fields"]["output"]:
                for kind, file in previous["verified"].items():
                    if re.fullmatch(r"[0-9a-f]{64}", file.get("sha256", "")):
                        verified[file["file_token"]] = (kind, file["name"], file["size"])
            elif "field_id" not in previous:  # Earlier WorkBuddy archive receipts.
                steps = previous["steps"]
                final = {file["file_token"]: file for file in steps["final"]["attachments"]}
                for kind, token in steps["readback_before_replace"]["new_tokens"].items():
                    file, prepared = final[token], steps["prepared"][kind]
                    proof = steps["readback_hash"][kind]
                    if (proof["matches"] is True and proof["sha256"] == prepared["sha256"]
                            and file["name"] == prepared["upload_name"] and file["size"] == prepared["bytes"]):
                        verified[token] = (kind, file["name"], file["size"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    result = set()
    for old in entry["existing_reports"]:
        token = old["file_token"]
        kind, name, size = verified.get(token, (None, None, None))
        if kind in entry["types"] and (name, size) == (old["name"], old["size"]):
            result.add(token)
    return result


def archive(connector, plan, entry, receipt, run_dir, stage, batch_name):
    path = run_dir / "archive.json"
    state = {"status": "in_progress", "record_id": entry["record_id"], "field_id": plan["fields"]["output"],
             "verified": {}, "removed": []}
    atomic_write_json(path, state)
    before = current_record(connector, plan, entry)
    targets = {}
    for kind, doc in receipt["documents"].items():
        name = f"鲸研-{REPORT_TITLES[kind]}-{batch_name.removeprefix('feishu-')}-{doc['sha256'][:12]}.docx"
        targets[kind] = (name, doc)

    def matches(files, name, doc):
        found = [file for file in files if file["name"] == name]
        if len(found) > 1 or (found and found[0]["size"] != doc["bytes"]):
            raise ValueError("归档格有同名冲突，请核对附件版本。")
        return found[0] if found else None

    missing = []
    for name, document in targets.values():
        if not matches(before, name, document):
            staged = stage / name
            shutil.copyfile(document["docx"], staged)
            if file_hash(staged) != document["sha256"]:
                raise ValueError("待上传附件与报告回执不一致。")
            missing.append("./" + staged.relative_to(connector.directory).as_posix())
    args = ["--record-id", entry["record_id"], "--field-id", state["field_id"]]
    if missing:
        connector.table_call("record-upload-attachment", plan, *args, *repeated("--file", missing))
    after = current_record(connector, plan, entry) if missing else before
    before_tokens = {file["file_token"] for file in before}
    after_tokens = {file["file_token"] for file in after}
    if not before_tokens <= after_tokens:
        raise ValueError("追加报告后原附件列表发生变化，请核对表格。")
    for kind, (name, doc) in targets.items():
        file = matches(after, name, doc)
        if file is None or connector.download(plan, entry["record_id"], file, stage / f"verify-{kind}.docx") != doc["sha256"]:
            raise ValueError(f"归档回读未通过：{REPORT_TITLES[kind]}")
        state["verified"][kind] = {**file, "sha256": doc["sha256"]}
        atomic_write_json(path, state)
    new_tokens = {file["file_token"] for file in state["verified"].values()}
    old = (prior_verified(run_dir, plan, entry) & after_tokens) - new_tokens if plan["replace"] else set()
    if old:
        latest = current_record(connector, plan, entry)
        latest_tokens = {file["file_token"] for file in latest}
        if not new_tokens <= latest_tokens:
            raise ValueError("新版报告在替换前发生变化，请核对归档格。")
        old &= latest_tokens
        if old:
            connector.table_call("record-remove-attachment", plan, *args, *repeated("--file-token", sorted(old)), "--yes")
            final = current_record(connector, plan, entry)
            final_tokens = {file["file_token"] for file in final}
            if not latest_tokens - old <= final_tokens or old & final_tokens:
                raise ValueError("替换后无法确认新版及其他附件均已保留。")
    retained = (set(file["file_token"] for file in entry["existing_reports"]) & after_tokens) - old - new_tokens
    state.update(status="archived", removed=sorted(old), retained_prior=len(retained), receipt=str(path))
    atomic_write_json(path, state)
    return state


def run(connector, config_file, config, root, args):
    path = inside(args.batch, root / "_batches")
    if path.name != "batch.json" or not path.parent.name.startswith("feishu-"):
        raise ValueError("请指定飞书清点命令返回的批次文件。")
    with exclusive_work(path.parent):
        plan = load_json(path)
        if plan.get("version") != "feishu-batch-v1":
            raise ValueError("此批次未由飞书清点命令创建。")
        pending = {"pending", "running", "archive_failed"}
        if args.retry_failed:
            pending.add("failed")
        entries = [entry for entry in plan["records"] if entry["status"] in pending][:args.limit]
        runtime = None
        base = identifier(plan["base_token"])
        table = identifier(plan["table_id"], "tbl")
        for entry in entries:
            identifier(entry["record_id"], "rec")
            project = inside(root / f"{base}_{table}" / entry["record_id"], root)
            run_dir = inside(project / path.parent.name, project)

            def checkpoint():
                save_plan(path, plan)

            with tempfile.TemporaryDirectory(prefix="jingyan-files-", dir=connector.directory) as files:
                stage = Path(files)
                phase = "download"
                try:
                    with exclusive_work(project / "_feishu_execution"):
                        entry["status"] = "running"
                        checkpoint()
                        timing = entry.setdefault("timing", {})
                        started = time.monotonic()
                        progress(project=entry["project_name"], step="核对并下载项目材料")
                        current_record(connector, plan, entry)
                        materials = fetch_materials(connector, config, plan, entry, run_dir, stage, checkpoint)
                        timing["download_seconds"] = round(time.monotonic() - started, 2)
                        phase = "generate"
                        receipt = recover_receipt(run_dir, entry, materials)
                        if receipt is None:
                            if runtime is None:
                                phase = "configure"
                                runtime = setup_runtime(config_file, config)
                                if not api_key(runtime[1]["api_key_env"]):
                                    raise ValueError("报告模型密钥未配置，请先完成鲸研模型设置。")
                                phase = "generate"
                            progress(project=entry["project_name"], step="生成并核对报告")
                            build = SimpleNamespace(types=entry["types"], taskbook=materials["taskbook"]["path"],
                                                    report=materials.get("report", {}).get("path"),
                                                    record_id=entry["record_id"], project_name=entry["project_name"])
                            receipt = generate(build, runtime[0], run_dir, config, runtime[1])
                            validate_receipt(receipt, run_dir, entry, materials)
                        entry["receipt"] = receipt["receipt"]
                        timing["generate_seconds"] = receipt["seconds"]
                        checkpoint()
                        if plan["archive_requested"]:
                            phase = "archive"
                            started = time.monotonic()
                            progress(project=entry["project_name"], step="归档并回读报告")
                            entry["archive"] = archive(connector, plan, entry, receipt, run_dir, stage, path.parent.name)
                            timing["archive_seconds"] = round(time.monotonic() - started, 2)
                        entry["status"] = "archived" if plan["archive_requested"] else "generated"
                        entry["reason"] = "已生成任务书版，履约审查缺阶段总结" if (
                            entry["types"] == ["forecast"] and "review" in entry["requested_types"]
                        ) else "已完成"
                        entry.pop("diagnostics", None)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError) as error:
                    entry["status"] = {"archive": "archive_failed", "configure": "pending"}.get(phase, "failed")
                    entry["reason"] = str(error)
                    if isinstance(error, WorkerFailure):
                        entry["diagnostics"] = str(error.directory)
                    if phase == "archive":
                        entry["archive"] = {"status": "failed", "receipt": str(run_dir / "archive.json")}
                checkpoint()
                progress(project=entry["project_name"], status=entry["status"], counts=plan["counts"])
                if phase == "configure":
                    break
        if set(plan["counts"]) <= {"generated", "archived"}:
            status = "completed"
        else:
            status = "incomplete"
        return {"status": status, **summarize(plan, path)}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("数量应为正整数。")
    return number


def target_arguments():
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--target", required=True)
    target.add_argument("--table")
    target.add_argument("--replace", action="store_true")
    target.add_argument("--whole-table", action="store_true")
    scope = target.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--project", action="append")
    scope.add_argument("--count", type=positive)
    scope.add_argument("--first", type=positive)
    for role in ALIASES:
        target.add_argument(f"--{role}-column")
    target.add_argument("--no-report", action="store_true")
    target.add_argument("--material", nargs=3, action="append", metavar=("RECORD", "ROLE", "ATTACHMENT"))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    metadata = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    version = re.search(r"(?m)^version:\s*(\S+)", metadata)
    skill_version = version[1] if version else "unknown"
    parser.add_argument("--version", action="version", version=skill_version)
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    target = target_arguments()
    deliver = commands.add_parser("deliver", parents=[target], help="生成两份项目报告并写回原记录")
    deliver.add_argument("--limit", type=positive)
    deliver.set_defaults(types=list(STANDARD_REPORT_TYPES), archive=True)
    scan = commands.add_parser("inspect", parents=[target], help="只读清点，保存指定交付方式的批次")
    scan.add_argument("--types", nargs="+", choices=REPORT_TITLES, default=list(STANDARD_REPORT_TYPES))
    delivery = scan.add_mutually_exclusive_group(required=True)
    delivery.add_argument("--archive", action="store_true", help="核对成稿后归档到飞书原记录")
    delivery.add_argument("--local-only", dest="archive", action="store_false", help="仅生成本地文稿")
    execute = commands.add_parser("run", help="执行已清点的批次，保留检查点")
    execute.add_argument("--batch", required=True, type=Path)
    execute.add_argument("--limit", type=positive)
    execute.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    try:
        config_file = args.config.resolve(strict=True)
        config = load_json(config_file)
        root = workspace(config_file, config)
        connector = Connector(Path.cwd().resolve())
        if args.command in {"inspect", "deliver"}:
            if args.replace and not args.archive:
                raise ValueError("替换旧报告需要本次归档请求。")
            if args.no_report and args.report_column:
                raise ValueError("请在指定阶段总结列和确认缺列之间选择。")
            result = inspect(connector, root, args)
            if args.command == "deliver":
                progress(step="清点完成，可按原批次续跑", batch=result["batch"], total=result["total"])
                planned = SimpleNamespace(batch=Path(result["batch"]), limit=args.limit, retry_failed=False)
                result = run(connector, config_file, config, root, planned)
        else:
            result = run(connector, config_file, config, root, args)
    except NeedsSelection as error:
        result = {"status": "needs_selection", "message": str(error), "candidates": error.candidates}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError) as error:
        result = {"status": "failed", "message": str(error)}
    print(json.dumps({"skill_version": skill_version, **result}, ensure_ascii=False))
    return {"planned": 0, "completed": 0, "needs_selection": 3, "incomplete": 3}.get(result["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
