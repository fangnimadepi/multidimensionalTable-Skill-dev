"""Validate source-bound host responses and render the established report layout."""

from pathlib import Path
import re

from host_materials import digest
from reading_copy import card_markdown, emphasize_lead, emphasize_review_summary
from project_report_contract import literal_quote, cell, asserts_delay
from project_report_findings import CATEGORIES
from project_report_presentation import number_support, presentation_numbers
from project_report_synthesis import unsupported_completion_claim

KINDS = {"计划要求", "阶段进展", "后续计划", "前期基础", "背景说明"}
SCOPES = {"本期", "后续", "总体", "待确认"}
UNIT_KINDS = {"事实", "计划", "分析", "建议", "示意", "材料表述"}
STATUSES = {"满足", "部分完成", "尚未完成", "待确认", "后续节点", "总体参考"}


def values(value, path, errors, minimum=0, maximum=300):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        errors.append(path + ":列表长度或格式不正确")
        return []
    return value


def prose(value, path, errors, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\n" in value or "\r" in value:
        detail = "；此处须填字符串，不是段落对象" if isinstance(value, dict) else ""
        errors.append(path + ":缺少完整文字或过长" + detail)
        return ""
    return value.strip()


def citations(raw, sources, path, errors, side=None):
    checked = []
    for index, cite in enumerate(values(raw, path, errors, 1, 30)):
        key = cite if isinstance(cite, str) else cite.get("source_id") if isinstance(cite, dict) else None
        source = sources.get(key) if isinstance(key, str) else None
        # Native agents select immutable source IDs; the program restores the
        # exact text. Legacy explicit excerpts remain subject to literal checks.
        quote = source["text"] if source and isinstance(cite, str) else literal_quote(cite.get("quote"), source["text"]) if source else None
        # A whole table cell can legitimately be just "6月" or "8B". Accept
        # that entire stable unit, never an arbitrary tiny span of a long block.
        raw_quote = cite.get("quote") if isinstance(cite, dict) else None
        if source and quote is None and isinstance(raw_quote, str) and raw_quote.strip() and raw_quote.strip() == source["text"].strip():
            quote = source["text"].strip()
        if not source or not quote or side and source["side"] != side:
            why = "未知来源编号 " + str(key) if not source else "来源应为" + side if side and source["side"] != side else "摘录不是原文；改用该来源编号即可回填真实原文"
            errors.append(f"{path}/{index}:出处不在本次材料中或摘录不是原文（{why}）")
            continue
        checked.append({"source_id": source["source_id"], "quote": quote})
    return checked


def numeric_feedback(content, support, path, label):
    missing = sorted(presentation_numbers(content) - presentation_numbers(support, chinese_counts=True))
    return path + ":" + label + "（缺少数字/规格：" + "、".join(missing[:20]) + "；补选含这些内容的来源或修正文句）"


def normalize_draft(raw, facts=None):
    """Accept the documented string rows or equivalent source-bound UNITs.

    WorkBuddy used UNITs in comparison rows despite the string example. Normalize
    their text without discarding conflicting bindings; canonical validation
    still enforces row-level facts, scope and all source checks.
    """
    import copy
    result, errors = copy.deepcopy(raw), []
    if not isinstance(result, dict) or not isinstance(result.get("review"), dict):
        return result, errors
    plans = {p["item_id"]: p for p in facts["plan_items"]} if facts else {}
    rows = result["review"].get("comparisons")
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        # Completion progress and a current-period compliance verdict are
        # different axes. A known overall/future condition always remains a
        # reference; preserve the model's progress label for the audit trail.
        identifier = row.get("item_id")
        if not isinstance(identifier, str):
            errors.append(f"review/comparisons/{index}:计划编号须为字符串")
            continue
        item = plans.get(identifier)
        row.pop("scope_note", None)
        if item and item["scope"] in {"总体", "后续"}:
            row["scope_note"] = "总体目标进展参考" if item["scope"] == "总体" else "后续节点进展参考"
            if row.get("status") in {"满足", "部分完成", "尚未完成"}:
                row["progress_status"] = row["status"]
                row["status"] = "总体参考" if item["scope"] == "总体" else "后续节点"
        progress = row.get("progress")
        if isinstance(progress, dict) and "fact_ids" not in row:
            row["fact_ids"] = progress.get("fact_ids", [])
        for field in ("progress", "judgement"):
            value = row.get(field)
            if not isinstance(value, dict):
                continue
            ids_p, ids_f = value.get("plan_ids", []), value.get("fact_ids", [])
            if (not isinstance(ids_p, list) or not isinstance(ids_f, list)
                    or any(not isinstance(k, str) for k in ids_p + ids_f)):
                errors.append(f"review/comparisons/{index}/{field}:来源编号须为数组")
                continue
            expected_p = [] if field == "progress" else [row.get("item_id")]
            if set(ids_p) != set(expected_p) or ids_f != row.get("fact_ids", []):
                errors.append(f"review/comparisons/{index}/{field}:段落来源须与本行计划和事实一致")
            if field == "progress" and (not isinstance(value.get("kind"), str) or value["kind"] not in {"事实", "材料表述"}) or field == "judgement" and value.get("kind") != "分析":
                errors.append(f"review/comparisons/{index}/{field}:段落类型应为" + ("事实或材料表述" if field == "progress" else "分析"))
            if field == "progress":
                if "progress_kind" in row and row["progress_kind"] != value.get("kind"):
                    errors.append(f"review/comparisons/{index}/progress:段落类型与本行 progress_kind 不一致")
                row["progress_kind"] = value.get("kind")
            row[field] = value.get("text")
    for finding in result["review"].get("findings", []) if isinstance(result["review"].get("findings"), list) else []:
        if not isinstance(finding, dict):
            continue
        finding.pop("scope_note", None)
        requirement = finding.get("requirement")
        ids = requirement.get("plan_ids", []) if isinstance(requirement, dict) and isinstance(requirement.get("plan_ids", []), list) else []
        if ids and all(isinstance(k, str) and k in plans and plans[k]["scope"] in {"总体", "后续"} for k in ids):
            scopes = {plans[k]["scope"] for k in ids}
            finding["scope_note"] = "总体目标进展参考" if scopes == {"总体"} else "后续节点进展参考" if scopes == {"后续"} else "总体或后续目标进展参考"
    return result, errors


def validate_facts(raw, source_list):
    errors, plans, facts = [], [], []
    if not isinstance(raw, dict):
        return {}, ["facts:需要 JSON 对象"]
    sources = {value["source_id"]: value for value in source_list}
    for index, item in enumerate(values(raw.get("plan_items"), "plan_items", errors)):
        path = f"plan_items/{index}"
        if not isinstance(item, dict):
            errors.append(path + ":需要对象")
            continue
        title = prose(item.get("title"), path + "/title", errors, 200)
        requirement = prose(item.get("requirement"), path + "/requirement", errors)
        cites = citations(item.get("citations"), sources, path + "/citations", errors, "plan")
        scope = item.get("scope")
        if not isinstance(scope, str) or scope not in SCOPES:
            errors.append(path + ":阶段范围不正确")
        node = item.get("node", "")
        support = "\n".join(c["quote"] for c in cites)
        if not isinstance(node, str) or len(node) > 120 or node and not any(node.strip() in c["quote"] for c in cites):
            errors.append(path + ":计划节点须摘自所引原文，找不到则留空")
        if not number_support(title + requirement, support):
            errors.append(numeric_feedback(title + requirement, support, path, "计划数字或规格不在所引原文中"))
        value = {"title": title, "requirement": requirement, "node": node, "scope": scope, "citations": cites}
        plans.append({"item_id": "P-" + digest(value)[:16], **value})
    for index, item in enumerate(values(raw.get("facts"), "facts", errors)):
        path = f"facts/{index}"
        if not isinstance(item, dict):
            errors.append(path + ":需要对象")
            continue
        content = prose(item.get("text"), path + "/text", errors)
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            errors.append(path + ":事实类别不正确")
        cites = citations(item.get("citations"), sources, path + "/citations", errors)
        if kind == "阶段进展" and any(sources[c["source_id"]]["side"] != "report" or
                sources[c["source_id"]]["role"] in {"target", "proof"} for c in cites):
            errors.append(path + ":实际进展不能由任务书、报告目标转述或证明材料名称代替")
        if kind == "后续计划" and any(sources[c["source_id"]]["side"] != "report" for c in cites):
            errors.append(path + ":阶段报告的下一步安排须来自报告")
        if not number_support(content, "\n".join(c["quote"] for c in cites)):
            errors.append(numeric_feedback(content, "\n".join(c["quote"] for c in cites), path, "事实数字或规格不在所引原文中"))
        value = {"text": content, "kind": kind, "citations": cites}
        facts.append({"fact_id": "F-" + digest(value)[:16], **value})
    required = {s["source_id"] for s in source_list if s["plan_candidate"]}
    coverage, seen = {}, []
    for item in values(raw.get("coverage"), "coverage", errors, 0, 1000):
        if not isinstance(item, dict):
            errors.append("coverage:需要对象")
            continue
        key = item.get("source_id")
        if not isinstance(key, str):
            errors.append("coverage:来源编号须为文字")
            continue
        seen.append(key)
        disposition = item.get("disposition")
        if key not in required or not isinstance(disposition, str) or disposition not in {"requirements", "not_requirement"}:
            errors.append("coverage:未知来源或分类")
        else:
            coverage[key] = item["disposition"]
            if item["disposition"] == "not_requirement":
                prose(item.get("reason"), "coverage/reason", errors, 300)
    if len(seen) != len(set(seen)) or set(seen) != required:
        errors.append("coverage:必须逐一覆盖本次全部计划候选来源，不能省略")
    cited = {c["source_id"] for value in plans for c in value["citations"]}
    for key, disposition in coverage.items():
        if disposition == "requirements" and key not in cited:
            errors.append("coverage:标为计划要求的来源未提取计划条件:" + key)
    return {"plan_items": plans, "facts": facts, "coverage": coverage}, sorted(set(errors))


def merge_facts(parts):
    result = {"plan_items": [], "facts": []}
    for name, key in (("plan_items", "item_id"), ("facts", "fact_id")):
        unique = {}
        for part in parts:
            for value in part[name]:
                unique[value[key]] = value
        result[name] = list(unique.values())
    return result


def claim_support(plan_ids, fact_ids, facts, sources):
    plans = {item["item_id"]: item for item in facts["plan_items"]}
    actual = {item["fact_id"]: item for item in facts["facts"]}
    cited = [c for key in plan_ids for c in plans[key]["citations"]]
    cited += [c for key in fact_ids for c in actual[key]["citations"]]
    cited = [{"source_id": c, "quote": sources[c]["text"]} if isinstance(c, str) else c for c in cited]
    unique = {digest(c): c for c in cited}
    return [{**c, "side": sources[c["source_id"]]["side"], "location": sources[c["source_id"]]["location"]}
            for c in unique.values()]


def validate_draft(raw, facts, catalog, types):
    errors, claims = [], []
    if not isinstance(raw, dict) or set(raw) != set(types):
        return [], ["draft:必须完整返回本次所需的全部报告类型，不能只交一份"]
    plans = {item["item_id"]: item for item in facts["plan_items"]}
    actual = {item["fact_id"]: item for item in facts["facts"]}
    sources = {s["source_id"]: s for s in catalog["sources"]}

    def unit(value, path, role=None):
        if not isinstance(value, dict):
            errors.append(path + ":需要有出处的段落对象")
            return
        content = prose(value.get("text"), path + "/text", errors)
        ids_p = values(value.get("plan_ids"), path + "/plan_ids", errors, 0, 100)
        ids_f = values(value.get("fact_ids"), path + "/fact_ids", errors, 0, 100)
        if (any(not isinstance(k, str) or k not in plans for k in ids_p)
                or any(not isinstance(k, str) or k not in actual for k in ids_f) or not (ids_p or ids_f)):
            errors.append(path + ":需要引用本次已提取的计划或事实编号")
            return
        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in UNIT_KINDS:
            errors.append(path + ":段落类型不正确")
            return
        if kind == "事实" or role == "actual":
            def report_context(item):
                cites = item["citations"]
                keys = [c if isinstance(c, str) else c["source_id"] for c in cites]
                return (item["kind"] == "背景说明" and bool(keys)
                        and bool(re.search(r"项目(?:于|执行期|编号|负责人|经理人|承担单位)|报告日期|启动", item["text"]))
                        and not re.search(r"计划|预期|目标|考核|指标|下一步", item["text"])
                        and all(sources[key]["side"] == "report" and sources[key]["role"] in {"body", "actual"} for key in keys))

            if (not ids_f or ids_p or not any(actual[k]["kind"] == "阶段进展" for k in ids_f)
                    or any(actual[k]["kind"] != "阶段进展" and not report_context(actual[k]) for k in ids_f)):
                errors.append(path + ":实际完成叙述须含报告当前进展，可辅引报告背景；不能借用计划数字、目标转述、下一步或前期基础")
        if kind == "材料表述":
            # A report's wording about a target or next step is evidence of what
            # the report says, not evidence that the target was achieved.
            keys = [c if isinstance(c, str) else c["source_id"]
                    for key in ids_f for c in actual[key]["citations"]]
            if role != "statement":
                errors.append(path + ":材料表述只用于报告措辞或后续安排对照，不能代替正文的实际成果")
            if (ids_p or not ids_f or not keys or any(sources[k]["side"] != "report"
                    or sources[k]["role"] == "proof" for k in keys)):
                errors.append(path + ":材料表述只可引用报告正文或报告表格，不能据此认定实际完成")
        if kind == "计划" or role == "plan":
            if not ids_p or ids_f:
                errors.append(path + ":计划叙述须引用任务书计划条件")
        if role == "difference" and (not ids_p or not ids_f):
            errors.append(path + ":出入情况需同时引用计划条件和报告事实")
        if types == ["forecast"] and (kind == "事实" or ids_f and any(actual[k]["kind"] == "阶段进展" for k in ids_f)):
            errors.append(path + ":任务书版不能写成本项目已实现的阶段成果")
        support = claim_support(ids_p, ids_f, facts, sources)
        # For current-result prose, only numbers explicitly extracted into its
        # selected report facts qualify. A long source unit may also contain a
        # future 32B goal alongside an actual 8B result; do not borrow that goal.
        numeric_support = "\n".join(actual[k]["text"] for k in ids_f) if kind in {"事实", "材料表述"} or role == "actual" else "\n".join(c["quote"] for c in support)
        if not number_support(content, numeric_support):
            errors.append(numeric_feedback(content, numeric_support, path, "数字或规格缺少所选原文支持"))
        if unsupported_completion_claim(content):
            errors.append(path + ":不能扩展为正式验收、真实性或整体履约认定")
        if asserts_delay(content):
            dated = any(c["side"] == "report" and re.search(r"截至\s*20\d{2}", c["quote"]) for c in support)
            if not dated or not ids_p:
                errors.append(path + ":逾期判断需要明确计划节点和报告事实截至时间")
        if re.search(r"核对条目|尚待拆解|校验降级|模型输出|semantic_reviewed", content):
            errors.append(path + ":正文含开发过程术语")
        claims.append({"claim_id": "C-" + digest([path, content])[:16], "path": path, "text": content,
                       "kind": kind, "plan_ids": ids_p, "fact_ids": ids_f, "sources": support})

    def paragraphs(raw, path, role=None, minimum=1, maximum=8):
        for index, value in enumerate(values(raw, path, errors, minimum, maximum)):
            unit(value, f"{path}/{index}", role)

    if "review" in types:
        report = raw["review"]
        if not isinstance(report, dict):
            return [], ["review:缺少履约报告对象"]
        for name, role in (("overview", None), ("plan_summary", "plan"), ("progress_summary", "actual"), ("conclusion", None)):
            paragraphs(report.get(name), "review/" + name, role, maximum=2 if name in {"overview", "conclusion"} else 4)
        compared = []
        comparison_rows = values(report.get("comparisons"), "review/comparisons", errors, 1, max(1, len(plans)))
        for index, row in enumerate(comparison_rows):
            path = f"review/comparisons/{index}"
            if not isinstance(row, dict) or not isinstance(row.get("item_id"), str) or row["item_id"] not in plans:
                errors.append(path + ":计划编号无效")
                continue
            item = plans[row["item_id"]]
            compared.append(row["item_id"])
            status = row.get("status")
            if not isinstance(status, str) or status not in STATUSES:
                errors.append(path + ":对照状态无效")
                continue
            if item["scope"] in {"后续", "总体"} and status not in {"后续节点", "总体参考", "待确认"}:
                errors.append(path + ":后续或总体目标不能直接作为本期未达或达标判断")
            if item["scope"] in {"本期", "待确认"} and status in {"后续节点", "总体参考"}:
                errors.append(path + ":不能把本期或未明范围的条件移出对照")
            ids = row.get("fact_ids", [])
            progress_kind = row.get("progress_kind", "事实")
            if not isinstance(progress_kind, str) or progress_kind not in {"事实", "材料表述"}:
                errors.append(path + ":progress_kind 只能是事实或材料表述")
            if progress_kind == "材料表述" and (status in {"满足", "部分完成", "尚未完成"}
                    or isinstance(row.get("progress_status"), str) and row["progress_status"] in {"满足", "部分完成", "尚未完成"}):
                errors.append(path + ":材料表述不能作为明确完成状态，需改用报告当前进展或标待确认")
            if status in {"满足", "部分完成", "尚未完成"} and not ids:
                errors.append(path + ":明确进展判断需要报告当前事实")
            if ids:
                unit({"text": row.get("progress"), "fact_ids": ids, "plan_ids": [], "kind": progress_kind},
                     path + "/progress", "actual" if progress_kind == "事实" else "statement")
            elif not isinstance(row.get("progress", ""), str):
                errors.append(path + ":进展须为文字")
            unit({"text": row.get("judgement"), "fact_ids": ids, "plan_ids": [item["item_id"]], "kind": "分析"}, path + "/judgement")
        if len(compared) != len(set(compared)) or set(compared) != set(plans):
            errors.append("review/comparisons:必须对已提取的全部计划条件逐项登记，未知项标待确认")
        for index, finding in enumerate(values(report.get("findings"), "review/findings", errors)):
            path = f"review/findings/{index}"
            if not isinstance(finding, dict):
                errors.append(path + ":需要对象")
                continue
            if finding.get("category") not in CATEGORIES:
                errors.append(path + ":差异类别无效")
            unit(finding.get("title"), path + "/title")
            unit(finding.get("requirement"), path + "/requirement", "plan")
            value = finding.get("progress")
            unit(value, path + "/progress", "statement" if isinstance(value, dict) and value.get("kind") == "材料表述" else "actual")
            unit(finding.get("difference"), path + "/difference", "difference")
        paragraphs(report.get("pi_questions"), "review/pi_questions", minimum=0, maximum=40)
        paragraphs(report.get("manager_notes"), "review/manager_notes", minimum=0, maximum=8)
        if any(row.get("status") == "待确认" for row in comparison_rows if isinstance(row, dict)) and not report.get("pi_questions") and not report.get("manager_notes"):
            errors.append("review:待确认的计划条件需在确认话术或经理人提示中说明")
        pending_ids = {row.get("item_id") for row in comparison_rows if isinstance(row, dict) and row.get("status") == "待确认"
                       and isinstance(row.get("item_id"), str)}
        addressed = {key for name in ("pi_questions", "manager_notes") for u in (report.get(name) if isinstance(report.get(name), list) else [])
                     if isinstance(u, dict) and isinstance(u.get("plan_ids"), list) for key in u["plan_ids"] if isinstance(key, str)}
        if not pending_ids <= addressed:
            errors.append("review:待确认计划条件没有对应的确认事项")
        if report.get("findings") and not report.get("pi_questions"):
            errors.append("review:出入事项需要可直接使用的 PI 确认话术")

    for kind in (name for name in types if name != "review"):
        report = raw[kind]
        if not isinstance(report, dict):
            errors.append(kind + ":缺少报告对象")
            continue
        unit(report.get("value_title"), kind + "/value_title")
        if kind == "forecast":
            paragraphs(report.get("overview"), kind + "/overview", maximum=2)
        for index, row in enumerate(values(report.get("fact_sheet"), kind + "/fact_sheet", errors, 1, 8)):
            if isinstance(row, dict):
                prose(row.get("label"), kind + "/fact_sheet/label", errors, 100)
                unit(row.get("content"), f"{kind}/fact_sheet/{index}")
            else:
                errors.append(kind + "/fact_sheet:需要对象")
        paragraphs(report.get("short_version"), kind + "/short_version", maximum=4)
        paragraphs(report.get("long_intro"), kind + "/long_intro", maximum=2)
        for index, section in enumerate(values(report.get("long_sections"), kind + "/long_sections", errors, 3, 6)):
            if not isinstance(section, dict):
                errors.append(kind + "/long_sections:需要对象")
                continue
            unit(section.get("title"), f"{kind}/long_sections/{index}/title")
            paragraphs(section.get("paragraphs"), f"{kind}/long_sections/{index}/paragraphs", maximum=4)
        paragraphs(report.get("next_steps"), kind + "/next_steps", maximum=2)
        card = report.get("card")
        if not isinstance(card, dict) or set(card) != {"delivered", "method", "value", "stage", "next"}:
            errors.append(kind + "/card:需要完整的五项可编辑卡片文字")
        else:
            for key, value in card.items():
                unit(value, kind + "/card/" + key)
    return claims, sorted(set(errors))


def validate_check(raw, claims, paired):
    if not isinstance(raw, dict):
        return ["check:需要对象"]
    rows = raw.get("checks")
    expected = {value["claim_id"] for value in claims}
    if (not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("claim_id"), str) for row in rows)
            or len(rows) != len(expected) or {r.get("claim_id") for r in rows} != expected
            or any(type(row.get("supported")) is not bool for row in rows)):
        return ["check:必须逐项返回本次全部陈述的检查结果，不增删或重复编号"]
    errors = [row["claim_id"] + ":" + str(row.get("reason") or "与来源不符")[:500]
              for row in rows if not row["supported"]]
    for field in ("plan_coverage_ok", "stage_scope_ok"):
        if raw.get(field) is not True:
            errors.append("check:" + field + ":未通过")
    if paired and raw.get("cross_report_consistent") is not True:
        errors.append("check:两份报告的成果状态或口径不一致")
    issues = raw.get("issues")
    if not isinstance(issues, list):
        errors.append("check:缺少 issues 数组")
    else:
        errors += ["check:" + str(value)[:500] for value in issues]
    return errors


def brief_locations(items):
    """Group page ranges/sections instead of printing every paragraph location."""
    sides = []
    for side, label in (("plan", "任务书"), ("report", "总结报告")):
        selected = [s for s in items if s["side"] == side]
        if not selected:
            continue
        pages, sections = set(), []
        for source in selected:
            location = source["location"]
            page = re.search(r"原 PDF 第(\d+)页", location)
            if page:
                pages.add(int(page.group(1)))
            else:
                heading = source.get("heading")
                if heading == "项目目标与计划条件":
                    heading = "目标、考核指标与里程碑安排"
                value = heading or location
                if value not in sections:
                    sections.append(value)
        spans = []
        for page in sorted(pages):
            if spans and page == spans[-1][-1] + 1:
                spans[-1].append(page)
            else:
                spans.append([page])
        if spans:
            sections.insert(0, "第" + "、".join(str(p[0]) if len(p) == 1 else f"{p[0]}—{p[-1]}" for p in spans) + "页")
        sides.append(label + "：" + "、".join(sections[:4]) + ("等相关章节" if len(sections) > 4 else ""))
    return "；".join(sides)


def brief_appendix(kind, report, claims):
    groups = {}
    for claim in claims:
        path = claim["path"]
        if not path.startswith(kind + "/"):
            continue
        if "/findings/" in path:
            number = int(path.split("/")[2])
            finding = report.get("findings", [])[number]
            label = finding["category"] + "的核对位置"
        elif "/plan_summary" in path:
            label = "任务书目标与计划"
        elif "/progress_summary" in path or "/fact_sheet/" in path:
            label = "预期成果与用途" if kind == "forecast" else "代表成果与关键指标"
        elif "/next_steps/" in path:
            label = "后续安排"
        else:
            continue
        groups.setdefault(label, []).extend(claim["sources"])
    return ["- **" + label + "：** " + brief_locations(items) for label, items in groups.items() if items]


def render(kind, draft, facts, catalog, title, paths, claims):
    from feishu_report_bundle import REPORT_TITLES
    lines = ["# " + REPORT_TITLES[kind], "", "**项目：" + title + "**", "",
             "| 项目信息 | 内容 |", "| --- | --- |"]
    labels = {"project_number": "项目编号", "organization": "承担单位", "principal_investigator": "项目负责人",
              "project_manager": "项目经理人", "execution_period": "项目执行期"}
    for key, label in labels.items():
        if key in catalog["profile"]:
            lines.append("| " + label + " | " + cell(catalog["profile"][key]["value"]) + " |")
    if kind == "forecast":
        lines += ["| 材料口径 | 任务书版预期成果，实际进展待后续材料核对 |", ""]
    else:
        lines += ["| 审查阶段与基准 | " + cell(catalog["review_stage"] + "；" + catalog["checkpoint_date"] + "（暂定）") + " |", ""]
    lines += ["任务书：" + Path(paths["plan"]).name, ""]
    if "report" in paths:
        lines += ["总结报告：" + Path(paths["report"]).name, ""]

    def paras(items):
        for value in items:
            lines.extend([value["text"], ""])

    plans = {item["item_id"]: item for item in facts["plan_items"]}
    report = draft[kind]
    card = None
    if kind == "review":
        lines += ["## 一、项目概况与审查口径", ""]
        paras(report["overview"])
        lines += ["## 二、计划与实际进展", "", "**任务书计划**", ""]
        paras(report["plan_summary"])
        lines += ["**实际进展**", ""]
        paras(report["progress_summary"])
        visible = [row for row in report["comparisons"] if row["status"] in {"满足", "部分完成", "尚未完成"}]
        lines += ["### 计划与进展对照", ""]
        if visible:
            lines += ["| 任务书要求 | 计划节点 | 总结报告所述 | 审查判断 |", "| --- | --- | --- | --- |"]
            for row in visible:
                item = plans[row["item_id"]]
                lines.append("| " + " | ".join(cell(v) for v in (item["requirement"], item["node"], row["progress"], row["judgement"])) + " |")
        else:
            lines += ["现有材料尚不足以形成明确的逐项对照，需要确认的内容列在下方。"]
        lines += ["", "## 三、项目出入点与对接重点", ""]
        if not report["findings"]:
            lines += ["本节暂无单列的出入事项。", ""]
        for category in CATEGORIES:
            selected = [value for value in report["findings"] if value["category"] == category]
            if not selected:
                continue
            lines += ["### " + category, ""]
            for index, finding in enumerate(selected, 1):
                lines += ["#### " + str(index) + ". " + finding["title"]["text"], ""]
                if finding.get("scope_note"):
                    lines += ["**计划范围：** " + finding["scope_note"], ""]
                progress_label = "报告所述" if finding["progress"].get("kind") == "材料表述" else "报告完成情况"
                for field, label in (("requirement", "任务书要求"), ("progress", progress_label), ("difference", "出入情况")):
                    lines += ["**" + label + "：** " + finding[field]["text"], ""]
        lines += ["### 综合审查意见", ""]
        paras(report["conclusion"])
        if report["pi_questions"]:
            lines += ["### 与 PI 老师确认事项", ""]
            paras(report["pi_questions"])
        if report["manager_notes"]:
            lines += ["**项目经理人需确认**", ""]
            paras(report["manager_notes"])
    else:
        if kind == "forecast":
            lines += ["## 一、目标成果提炼", "", "| 预期成果 | 计划交付与关键指标 | 计划节点 |", "| --- | --- | --- |"]
            lines += ["| " + " | ".join(cell(item[k]) for k in ("title", "requirement", "node")) + " |" for item in plans.values()]
            lines += ["", "## 二、成果表达", "", "### 项目概述", ""]
            paras(report["overview"])
        else:
            lines += ["## 一、事实成果提炼", "", "| 核心问题 | 事实提炼 |", "| --- | --- |"]
            lines += ["| " + cell(row["label"]) + " | " + cell(row["content"]["text"]) + " |" for row in report["fact_sheet"]]
        lines += ["", "### 简版" if kind == "forecast" else "## 二、简版", "", "#### " + report["value_title"]["text"], ""]
        for index, value in enumerate(report["short_version"]):
            lines += [emphasize_lead(value["text"]) if index == 0 else value["text"], ""]
        lines += ["### 科普长版" if kind == "forecast" else "## 三、科普长版", ""]
        paras(report["long_intro"])
        for section in report["long_sections"]:
            lines += [("#### " if kind == "forecast" else "### ") + section["title"]["text"], ""]
            paras(section["paragraphs"])
        lines += ["### 后续验证" if kind == "forecast" else "### 后续推进判断", ""]
        paras(report["next_steps"])
        card = {key: value["text"] for key, value in report["card"].items()}
        card.update(title=report["value_title"]["text"], project_name=title, forecast=kind == "forecast",
                    heading="三 横版展示卡片" if kind == "forecast" else "四 横版展示卡片")
        lines += card_markdown(card)
    lines += ["## 材料来源与重点定位", ""]
    lines += brief_appendix(kind, report, claims)
    markdown = "\n".join(lines).strip() + "\n"
    return (emphasize_review_summary(markdown) if kind == "review" else markdown), card
