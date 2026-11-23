#!/usr/bin/env python3
"""Re-export an existing checked result without any model or OCR requests."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
import uuid

from local_reports import checked_reports, load_json, setup_runtime, setup_host_runtime, source_path, workspace
from read_docx import file_hash, inside


def reexport(args):
    started = time.monotonic()
    config_file = args.config.resolve(strict=True)
    config = load_json(config_file)
    run_dir = inside(args.run_dir, workspace(config_file, config))
    receipt_path = inside(args.receipt, run_dir / "reports")
    previous = load_json(receipt_path)
    if previous.get("status") != "checked":
        raise ValueError("只能重新导出已经通过检查的报告。")
    paths = {"plan": source_path(run_dir, args.taskbook, "taskbook", config)}
    if args.report:
        paths["report"] = source_path(run_dir, args.report, "report", config)
    hashes = {role: file_hash(path) for role, path in paths.items()}
    if hashes != previous.get("material_hashes"):
        raise ValueError("材料已变化，请重新生成，不能沿用旧结论导出。")
    if previous.get("execution_mode") == "host":
        setup_host_runtime(config_file, config)
        from host_reports import reexport as host_reexport
        return host_reexport(previous, run_dir, paths)
    root, _ = setup_runtime(config_file, config, use_credentials=False)

    from report_worker import pipeline_fingerprint
    from feishu_report_bundle import REPORT_TITLES
    from reading_copy import compose
    from word_reports import render_docx
    from review_common import atomic_write_json

    diagnostics = inside(Path(previous["diagnostics"]), root / "runs")
    result = load_json(diagnostics / "generated.json")
    types = list(previous["documents"])
    markdown = checked_reports(result, types, hashes, pipeline_fingerprint())
    if previous["project_id"] != result["projectId"]:
        raise ValueError("项目与已检查底稿不一致，停止导出。")
    output_root = inside(run_dir / "reports", run_dir)
    output = output_root / ("reexport-" + uuid.uuid4().hex[:12])
    with tempfile.TemporaryDirectory(dir=output_root, prefix=".export-") as temporary:
        draft = Path(temporary) / "documents"
        draft.mkdir()
        documents = {}
        for kind, original in markdown.items():
            structured = None
            entry = result.get("reportFiles", {}).get(kind)
            if entry:
                snapshot = inside(root / entry["path"], root / "runs")
                if file_hash(snapshot) != entry["sha256"]:
                    raise ValueError("已检查的报告底稿发生变化，停止导出。")
                structured = load_json(snapshot)
            body, appendix, card = compose(kind, original, structured)
            content = body.rstrip() + "\n\n" + appendix
            md = draft / (REPORT_TITLES[kind] + ".md")
            md.write_text(content, encoding="utf-8")
            word = md.with_suffix(".docx")
            stats = render_docx(content, word, title=REPORT_TITLES[kind], card=card)
            documents[kind] = {"docx": str(output / word.name), "markdown": str(output / md.name),
                               "sha256": stats["sha256"], "bytes": stats["bytes"],
                               "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               "internal_links": stats["internal_links"], "bookmarks": stats["bookmarks"], "card": stats["card"]}
        if any(file_hash(path) != hashes[role] for role, path in paths.items()):
            raise ValueError("导出期间材料发生变化，停止交付。")
        receipt = {**previous, "documents": documents, "checks": result["checks"],
                   "source_receipt": str(receipt_path), "export_only": True, "new_model_calls": 0,
                   "seconds": round(time.monotonic() - started, 2), "receipt": str(output / "checked.json")}
        atomic_write_json(draft / "checked.json", receipt)
        draft.rename(output)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--taskbook", required=True)
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        result = reexport(args)
    except (OSError, ValueError, KeyError, ImportError, RuntimeError, TypeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
