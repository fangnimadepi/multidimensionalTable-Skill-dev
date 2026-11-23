#!/usr/bin/env python3
"""Route a material inventory before downloading or invoking report models."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


def route(record, requested):
    taskbooks = record.get("taskbook", [])
    reports = record.get("report", [])
    if not isinstance(taskbooks, list) or not isinstance(reports, list):
        raise ValueError("每条记录的任务书和总结报告应为附件列表。")
    missing = [role for role, attachments in (("taskbook", taskbooks), ("report", reports)) if not attachments]
    result = {"record_id": record["record_id"], "project_name": record.get("project_name", ""),
              "taskbook": taskbooks, "report": reports, "existing_reports": record.get("existing_reports", []),
              "requested_types": requested, "types": [], "missing_roles": missing, "status": "pending"}
    if record.get("input_error"):
        result.update(status="blocked", reason=record["input_error"])
    elif not taskbooks:
        result.update(status="blocked", reason="缺少项目任务书")
    elif len(taskbooks) != 1 or (requested != ["forecast"] and len(reports) > 1):
        result.update(status="needs_selection", reason="存在同角色的多份材料，需要选定本次版本")
    elif not str(taskbooks[0].get("name", "")).lower().endswith(".docx"):
        result.update(status="needs_conversion", reason="任务书需转为可读取的 DOCX")
    elif requested == ["forecast"]:
        result.update(types=["forecast"], reason="按用户指定生成任务书版预期成果分析")
    elif not reports:
        if "achievement" in requested:
            result.update(types=["forecast"], reason="缺少阶段总结，生成任务书版预期成果分析；履约审查待补材料")
        else:
            result.update(status="blocked", reason="缺少阶段总结，履约审查待补材料")
    elif not str(reports[0].get("name", "")).lower().endswith((".docx", ".pdf")):
        result.update(status="needs_conversion", reason="阶段总结需转为 DOCX 或 PDF")
    else:
        result.update(types=requested, reason="材料可进入下载及解析")
    return result


def make_plan(inventory, requested, group_size):
    if (not requested or len(set(requested)) != len(requested)
            or set(requested) - {"review", "achievement", "forecast"}
            or ("forecast" in requested and requested != ["forecast"])):
        raise ValueError("报告类型重复或组合不适用。")
    records = inventory["records"]
    ids = [record.get("record_id") for record in records]
    if any(not isinstance(key, str) or not key for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("项目记录标识缺失或重复，请核对全部分页。")
    entries = []
    for index, record in enumerate(records):
        entry = route(record, requested)
        entry.update(group=index // group_size + 1, steps={}, archive={})
        entries.append(entry)
    signature = hashlib.sha256(json.dumps(inventory, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"version": "batch-v1", "base_token": inventory["base_token"], "table_id": inventory["table_id"],
            "fields": inventory["fields"], "inventory_sha256": signature, "requested_types": requested,
            "group_size": group_size, "project_workers": 1, "total": len(entries),
            "counts": dict(Counter(entry["status"] for entry in entries)), "records": entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--types", nargs="+", required=True, choices=("review", "achievement", "forecast"))
    parser.add_argument("--group-size", type=int, choices=range(1, 21), default=5)
    args = parser.parse_args()
    try:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        plan = make_plan(inventory, args.types, args.group_size)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Resume the recorded plan instead of silently discarding completed work.
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        print(json.dumps({"status": "exists", "message": "批次记录已存在，请核对源材料后继续原批次。"}, ensure_ascii=False))
        return 3
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "planned", "total": plan["total"], "counts": plan["counts"],
                      "forecast": sum(entry["types"] == ["forecast"] for entry in plan["records"]),
                      "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
