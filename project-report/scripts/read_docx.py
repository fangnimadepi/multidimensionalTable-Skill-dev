"""Extract ordered DOCX text and tables inside one run directory using stdlib."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"


def inside(path, directory):
    resolved = path.resolve()
    if not resolved.is_relative_to(directory.resolve()):
        raise ValueError("文件路径超出本次项目目录。")
    return resolved


def paragraph_text(element):
    parts = []
    for node in element.iter():
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag == W + "tab" and not node.attrib:
            parts.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def block_elements(element):
    wrappers = {W + "sdt", W + "sdtContent", W + "customXml", W + "ins"}
    for child in element:
        if child.tag in wrappers:
            yield from block_elements(child)
        else:
            yield child


def blocks(element, prefix=""):
    result = []
    paragraph_count = 0
    table_count = 0
    for child in block_elements(element):
        if child.tag == W + "p":
            paragraph_count += 1
            result.append({"kind": "paragraph", "location": f"{prefix}P{paragraph_count}",
                           "text": paragraph_text(child)})
        elif child.tag == W + "tbl":
            table_count += 1
            location = f"{prefix}T{table_count}"
            rows = []
            for row_index, row in enumerate(child.findall(W + "tr"), 1):
                cells = []
                for cell_index, cell in enumerate(row.findall(W + "tc"), 1):
                    cell_location = f"{location}-R{row_index}-C{cell_index}"
                    properties = cell.find(W + "tcPr")
                    span = properties.find(W + "gridSpan") if properties is not None else None
                    merge = properties.find(W + "vMerge") if properties is not None else None
                    cells.append({
                        "location": cell_location,
                        "grid_span": span.get(W + "val", "1") if span is not None else "1",
                        "vertical_merge": merge.get(W + "val", "continue") if merge is not None else "",
                        "blocks": blocks(cell, cell_location + "-"),
                    })
                rows.append(cells)
            result.append({"kind": "table", "location": location, "rows": rows})
    return result


def text_lines(items):
    lines = []
    for item in items:
        if item["kind"] == "paragraph":
            lines.append(f'[{item["location"]}] {item["text"]}')
        else:
            lines.append(f'[{item["location"]}] 表格')
            for row in item["rows"]:
                for cell in row:
                    lines.append(f'[{cell["location"]}] 跨列={cell["grid_span"]} 纵向合并={cell["vertical_merge"]}')
                    lines.extend(text_lines(cell["blocks"]))
    return lines


def paragraphs(items):
    for item in items:
        if item["kind"] == "paragraph":
            yield item
        else:
            for row in item["rows"]:
                for cell in row:
                    yield from paragraphs(cell["blocks"])


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(run_dir, relative_input, role):
    root = run_dir.resolve(strict=True)
    raw = inside(root / "raw", root)
    path = inside(root / relative_input, raw)
    if not path.is_file():
        raise ValueError("输入文件不存在，请核对下载回执。")
    if path.suffix.lower() != ".docx":
        raise ValueError("该工具只读取 DOCX，请为其他格式使用已有的对应工具。")
    with ZipFile(path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        other_parts = [
            name for name in archive.namelist()
            if name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes", "word/comments"))
            and name.endswith(".xml")
        ]
    body = document.find(W + "body")
    if body is None:
        raise ValueError("DOCX 缺少正文结构。")
    items = blocks(body)
    special = {
        label: sum(1 for element in body.iter() if element.tag in tags)
        for label, tags in {
            "drawings_or_images": {W + "drawing", W + "pict"},
            "embedded_objects": {W + "object"},
            "math": {M + "oMath"},
            "textboxes": {W + "txbxContent"},
            "revisions": {W + "ins", W + "del"},
            "external_content": {W + "altChunk"},
        }.items()
    }
    rendered_lines = text_lines(items)
    has_text = any(item["text"].strip() for item in paragraphs(items))
    payload = {
        "file_name": path.name,
        "input_path": str(path),
        "sha256": file_hash(path),
        "text_extraction": "succeeded" if has_text else "no_text",
        "scope": "document_body",
        "location_kind": "DOCX逻辑段落及XML表格单元格，非排版页码",
        "body_objects_to_review": {key: value for key, value in special.items() if value},
        "other_document_parts_to_review": other_parts,
        "blocks": items,
    }
    output = inside(root / "extracted", root)
    output.mkdir(parents=True, exist_ok=True)
    json_path = inside(output / f"{role}.json", output)
    text_path = inside(output / f"{role}.txt", output)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    text_path.write_text("\n".join(rendered_lines) + "\n", encoding="utf-8")
    return {key: value for key, value in payload.items() if key != "blocks"} | {
        "json_path": str(json_path), "text_path": str(text_path),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--input", required=True, help="Path relative to run-dir, within raw/.")
    parser.add_argument("--role", required=True, choices=("taskbook", "report"))
    args = parser.parse_args()
    try:
        result = extract(args.run_dir, args.input, args.role)
    except (OSError, ValueError, KeyError, BadZipFile, ET.ParseError, RuntimeError) as error:
        print(json.dumps({"text_extraction": "failed", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["text_extraction"] == "succeeded" else 3


if __name__ == "__main__":
    sys.exit(main())
