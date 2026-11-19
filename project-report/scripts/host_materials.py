"""Prepare compact, source-bound reading units; retain exclusion provenance."""

import hashlib
import json
from pathlib import Path
import re
from datetime import date

from read_docx import paragraphs

PRIVATE = re.compile(r"联系电话|手机号码|^手机[：:]?(?:\+?86)?1\d{10}|电子邮箱|电子邮件|身份证|银行账号|开户银行|通讯地址")
HEAD = re.compile(r"^(?:[一二三四五六七八九十]+[、．.]|[1-9]\d?[、．.])\s*\S")
PLAN_SECTION = re.compile(r"里程碑|年度计划|年度目标|考核|成果指标|绩效目标|预期成果|成果形式|研究目标")
EXCLUDED = re.compile(r"单位基本情况表|企业基本情况|团队组成|项目组成员|人员名单|研究人员表|经费(?:预算|执行|决算)|项目预算|财务决算|合作单位意见|法定代表人的承诺|创新中心意见|委员会意见|填\s*写\s*说\s*明|编\s*报\s*要\s*求")
ADMIN_TABLE = re.compile(r"统一社会信用代码|注册地行政区划|法人代表情况|开户银行|银行账号|项目参加人员|人员基本情况|预算科目|实际发生数|经费支出明细")
PROTECTED = re.compile(r"里程碑|年度计划|年度目标|考核|成果指标|绩效目标|测评|验收|人才培养指标|知识产权指标")
ROLES = (("完成情况", "actual"), ("实际进展", "actual"), ("证明材料", "proof"),
         ("是否完成", "self_assessment"), ("自评", "self_assessment"),
         ("目标要求", "target"), ("考核要求", "target"), ("计划要求", "target"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text(items):
    return "\n".join(item["text"] for item in paragraphs(items) if item["text"].strip())


def parts(value, width=4000):
    start = 0
    while start < len(value):
        stop = min(len(value), start + width)
        if stop < len(value):
            breaks = list(re.finditer(r"[。；！？\n]", value[start:stop]))
            if breaks:
                stop = start + breaks[-1].end()
        yield start, stop, value[start:stop]
        start = stop


def heading_text(value):
    """Instructions beginning with 三、 are not persistent document headings."""
    value = value.strip()
    plain = re.sub(r"\s+", "", value)
    if len(plain) <= 45 and (HEAD.match(plain) or EXCLUDED.search(plain) or
                            re.search(r"项目目标|研究内容和|研究方案[、，]|项目的|阶梯追加|预期效益|相关说明|国内外合作", plain) or
                            plain in {"项目研究目标", "研究目标", "研究内容", "考核指标", "团队组成", "项目经费"}):
        return value
    return None


def compact_source(source):
    # A source's location and heading are stored once in materials.json, not
    # repeated on every model turn. Row context is essential for target/actual
    # cells and short milestone labels, so keep it when it adds information.
    result = {key: source[key] for key in ("source_id", "side", "text", "role", "plan_candidate")}
    context = source.get("context", "")
    if context and (source["role"] != "body" or source["side"] == "plan" and len(source["text"]) < 240):
        result["context"] = context
    return result


def catalog(extracted, profile, checkpoint="2026-08-31", stage="中期"):
    try:
        date.fromisoformat(checkpoint)
    except (ValueError, TypeError):
        raise ValueError("审查参考日期应为有效的 YYYY-MM-DD 日期。") from None
    if not isinstance(stage, str) or not stage.strip() or len(stage) > 40:
        raise ValueError("项目阶段名称无效。")
    sources, excluded = [], []

    def add(side, location, value, role, heading="", context="", candidate=False):
        value = "\n".join(line for line in value.splitlines() if not PRIVATE.search(re.sub(r"\s+", "", line))).strip()
        if not value:
            return
        for start, end, part in parts(value, width=1800):
            identity = [side, location, start, end, part]
            sources.append({"source_id": "S-" + digest(identity)[:16], "side": side, "location": location,
                            "text": part, "role": role, "heading": heading, "context": context,
                            "plan_candidate": candidate, "offset": [start, end]})

    for side, document in extracted.items():
        if "pages" in document:
            for page in document["pages"]:
                value = page["text"]
                # PDF columns may interleave. Never guess target/actual roles
                # or remove a page merely because it mentions a budget.
                add(side, "原 PDF 第" + str(page["page"]) + "页", value, "body",
                    candidate=side == "plan" and bool(PLAN_SECTION.search(value)))
            continue
        heading = ""
        for item in document["blocks"]:
            if item["kind"] == "paragraph":
                value = item["text"].strip()
                new_heading = heading_text(value)
                if new_heading:
                    heading = new_heading
                if EXCLUDED.search(re.sub(r"\s+", "", heading)) and not PROTECTED.search(value):
                    excluded.append({"side": side, "location": item["location"], "reason": "行政信息或填写说明", "heading": heading})
                    continue
                add(side, "正文段落 " + item["location"], value, "body", heading,
                    candidate=side == "plan" and bool(PLAN_SECTION.search(heading)))
                continue
            first = item["rows"][0] if item["rows"] else []
            labels = [text(cell["blocks"]) for cell in first]
            table_text = "\n".join(text(cell["blocks"]) for row in item["rows"] for cell in row)
            # Decide at table level only for recognizable administrative forms.
            # Never drop an opaque table or one containing acceptance conditions.
            admin = bool(ADMIN_TABLE.search(table_text) or EXCLUDED.search(re.sub(r"\s+", "", heading)))
            if admin and not PROTECTED.search(table_text):
                excluded.append({"side": side, "location": item["location"], "reason": "单位、人员或经费表", "heading": heading})
                continue
            # A taskbook often keeps its real section titles inside one large
            # cell. Such tables must reset a preceding 填写说明 heading.
            if side == "plan" and PLAN_SECTION.search(table_text):
                heading = "项目目标与计划条件"
            for number, row in enumerate(item["rows"], 1):
                row_context = "；".join(text(cell["blocks"])[:240] for cell in row[:1])
                for col, cell in enumerate(row):
                    value = text(cell["blocks"])
                    label = labels[col] if col < len(labels) else ""
                    role = next((role for marker, role in ROLES if marker in label), "body") if side == "report" else "body"
                    candidate = side == "plan" and bool(PLAN_SECTION.search(heading + " ".join(labels) + value))
                    add(side, "表格单元格 " + cell["location"], value, role, heading,
                        "列名：" + label[:240] + "；本行：" + row_context, candidate)
    if not any(value["side"] == "plan" for value in sources):
        raise ValueError("任务书未提取到可读正文，请检查文件。")
    # Generic taskbooks without the usual section headings still get a full
    # inventory pass; the host must classify each source rather than omit it.
    if not any(value["plan_candidate"] for value in sources):
        for value in sources:
            value["plan_candidate"] = value["side"] == "plan"
    profile = {key: {"value": value["value"], "sources": value.get("sources", [])}
               for key, value in profile.get("fields", {}).items()}
    return {"sources": sources, "excluded_sections": excluded, "profile": profile, "checkpoint_date": checkpoint,
            "review_stage": stage, "location_note": "DOCX 为逻辑段落或表格单元格，PDF 为原文件页序。"}


def batches(sources, characters=48000):
    chunk, size = [], 0
    for source in sources:
        amount = len(json.dumps(compact_source(source), ensure_ascii=False)) if "side" in source else len(json.dumps(source, ensure_ascii=False))
        if chunk and size + amount > characters:
            yield chunk
            chunk, size = [], 0
        chunk.append(source)
        size += amount
    if chunk:
        yield chunk
