"""Joint report writing and focused review; no per-paragraph fact-ID protocol."""

import copy
import re

from host_content import CATEGORIES, SCOPES, STATUSES, prose, values
from project_report_presentation import number_support, presentation_numbers
from project_report_synthesis import unsupported_completion_claim


def sources(material):
    result, counts = {}, {"plan": 0, "report": 0}
    for original in material["sources"]:
        side = original["side"]
        counts[side] += 1
        result[("A" if side == "plan" else "B") + str(counts[side])] = original
    return result


def required_plans(material):
    result = []
    for key, source in sources(material).items():
        value = re.sub(r"\s+", "", source["text"])
        label = len(value) < 65 and (re.fullmatch(r"[\d年月季度—–\-至/、：:]+", value)
                or re.fullmatch(r"[一二三四五六七八九十、.．\d]*项目的?里程碑[、，]?年度计划及年度目标.*", value)
                or value in {"年度", "年份", "时间", "年度计划", "年度目标", "考核指标", "研究目标", "项目研究目标", "预期成果", "研究内容", "项目成果形式、考核指标及测评方式"})
        condition = re.search(r"完成|构建|建立|建设|形成|实现|达到|提升|降低|减少|研发|开发|研制|开展|研究|验证|评估|发表|投稿|申请|提交|交付|发布|开源|培养|不少于|不低于|不高于|至少|≥|≤", value)
        if source["side"] == "plan" and source["plan_candidate"] and condition and value and not label:
            result.append(key)
    return result


def refs(value, lookup, path, errors, side=None, minimum=1):
    result = []
    for key in values(value, path, errors, minimum, 300):
        if not isinstance(key, str) or key not in lookup or side and lookup[key]["side"] != side:
            errors.append(path + ":来源编号无效或材料侧不正确，只使用本次 A/B 编号")
        elif key not in result:
            result.append(key)
    return result


def support(ids, lookup):
    return "\n".join(lookup[key]["text"] for key in ids)


def paragraphs(value, path, errors, minimum=1, maximum=8):
    for index, text in enumerate(values(value, path, errors, minimum, maximum)):
        prose(text, f"{path}/{index}", errors)


def normalize(raw):
    """Normalize formatting; never infer a semantic completion conclusion."""
    result = copy.deepcopy(raw)
    if not isinstance(result, dict) or not isinstance(result.get("plans"), list):
        return result
    for item in result["plans"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            item["id"] = item["id"].strip().upper()
    by_id = {p["id"]: p for p in result["plans"] if isinstance(p, dict) and isinstance(p.get("id"), str)}
    review = result.get("review")
    if isinstance(review, dict):
        for row in review.get("comparisons", []) if isinstance(review.get("comparisons"), list) else []:
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("plan"), str):
                row["plan"] = row["plan"].strip().upper()
            plan = by_id.get(row.get("plan")) if isinstance(row.get("plan"), str) else None
            if plan and isinstance(plan.get("scope"), str) and plan["scope"] in {"后续", "总体"}:
                row["status"] = "后续节点" if plan["scope"] == "后续" else "总体参考"
        for finding in review.get("findings", []) if isinstance(review.get("findings"), list) else []:
            if not isinstance(finding, dict):
                continue
            if isinstance(finding.get("plans"), list):
                finding["plans"] = [k.strip().upper() if isinstance(k, str) else k for k in finding["plans"]]
            if not finding.get("plans") and isinstance(finding.get("sources"), list):
                cited = {k for k in finding["sources"] if isinstance(k, str) and k.startswith("A")}
                matches = [p["id"] for p in by_id.values() if cited and isinstance(p.get("sources"), list)
                           and all(isinstance(k, str) for k in p["sources"]) and cited == set(p["sources"])]
                if len(matches) == 1:
                    finding["plans"] = matches
    return result


def validate(raw, material, types):
    errors = []
    if not isinstance(raw, dict):
        return ["compose:需要联合成稿对象"]
    missing_fields = {"plans", "key_facts", *types} - set(raw)
    if missing_fields:
        return [name + ":缺少本次要求的字段或报告，须补齐全部报告类型" for name in sorted(missing_fields)]
    lookup = sources(material)
    plans, seen, covered = {}, [], set()
    for index, p in enumerate(values(raw["plans"], "plans", errors, 1, 150)):
        path = f"plans/{index}"
        if not isinstance(p, dict):
            errors.append(path + ":需要计划对象")
            continue
        key = p.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"P[1-9]\d{0,3}", key) or key in plans:
            errors.append(path + ":使用互不重复的短编号 P1、P2")
            continue
        plans[key] = p
        prose(p.get("title"), path + "/title", errors, 180)
        requirement = prose(p.get("requirement"), path + "/requirement", errors)
        ids = refs(p.get("sources"), lookup, path + "/sources", errors, "plan")
        covered.update(ids)
        if not number_support(requirement, support(ids, lookup)):
            errors.append(path + "/requirement:计划数字或规格不在所选任务书原文中")
        if not isinstance(p.get("scope"), str) or p["scope"] not in SCOPES:
            errors.append(path + "/scope:范围为本期、后续、总体或待确认")
        if not isinstance(p.get("node", ""), str) or len(p.get("node", "")) > 120:
            errors.append(path + "/node:计划节点用短语，无则留空")
    missing = set(required_plans(material)) - covered
    if missing:
        errors.append("plans:计划条件遗漏来源 " + "、".join(sorted(missing)))
    for index, fact in enumerate(values(raw["key_facts"], "key_facts", errors, 0 if types == ["forecast"] else 1, 100)):
        path = f"key_facts/{index}"
        if not isinstance(fact, dict):
            errors.append(path + ":需要关键事实对象")
            continue
        content = prose(fact.get("text"), path + "/text", errors)
        ids = refs(fact.get("sources"), lookup, path + "/sources", errors,
                   "plan" if types == ["forecast"] else "report")
        if any(lookup[k]["role"] in {"target", "proof"} for k in ids) and types != ["forecast"]:
            errors.append(path + "/sources:当前关键成果不能引用报告目标或材料名称代替实际进展")
        if not number_support(content, support(ids, lookup)):
            errors.append(path + "/text:关键数字或规格缺少所选原文支持")
    if "review" in types:
        review = raw["review"]
        if not isinstance(review, dict):
            return errors + ["review:缺少履约审查报告"]
        for name in ("overview", "plan_summary", "progress_summary", "conclusion"):
            paragraphs(review.get(name), "review/" + name, errors, maximum=4)
        for name in ("pi_questions", "manager_notes"):
            paragraphs(review.get(name), "review/" + name, errors, minimum=0, maximum=30)
        rows = values(review.get("comparisons"), "review/comparisons", errors, 1, 150)
        for i, row in enumerate(rows):
            path = f"review/comparisons/{i}"
            if not isinstance(row, dict) or not isinstance(row.get("plan"), str) or row["plan"] not in plans:
                errors.append(path + ":计划编号无效")
                continue
            seen.append(row["plan"])
            status = row.get("status")
            if not isinstance(status, str) or status not in STATUSES:
                errors.append(path + "/status:对照状态无效")
                continue
            if isinstance(plans[row["plan"]].get("scope"), str) and plans[row["plan"]]["scope"] in {"本期", "待确认"} and status in {"后续节点", "总体参考"}:
                errors.append(path + "/status:本期或未明条件不能移出对照")
            ids = refs(row.get("sources", []), lookup, path + "/sources", errors, "report", minimum=0)
            progress = row.get("progress", "")
            if not isinstance(progress, str):
                errors.append(path + "/progress:进展用普通文字")
            if status in {"满足", "部分完成", "尚未完成"}:
                if not ids or not progress or all(lookup[k]["role"] in {"target", "proof"} for k in ids):
                    errors.append(path + ":明确进展判断需要报告当前内容，不能只引目标或证明材料名称")
            if ids and isinstance(progress, str) and not number_support(progress, support(ids, lookup)):
                errors.append(path + "/progress:关键进展数字缺少所选报告支持")
            prose(row.get("judgement"), path + "/judgement", errors)
        if len(seen) != len(set(seen)) or set(seen) != set(plans):
            errors.append("review/comparisons:全部计划条件须登记一次，未知项标待确认")
        for i, finding in enumerate(values(review.get("findings"), "review/findings", errors, 0, 100)):
            path = f"review/findings/{i}"
            if not isinstance(finding, dict):
                errors.append(path + ":需要出入事项对象")
                continue
            if not isinstance(finding.get("category"), str) or finding["category"] not in CATEGORIES:
                errors.append(path + "/category:差异类别无效")
            for field in ("title", "report", "difference"):
                prose(finding.get(field), path + "/" + field, errors)
            ids_p = finding.get("plans")
            if not isinstance(ids_p, list) or not ids_p or any(not isinstance(k, str) or k not in plans for k in ids_p):
                errors.append(path + "/plans:出入事项须关联任务书计划")
            ids = refs(finding.get("sources"), lookup, path + "/sources", errors, minimum=1)
            if not any(lookup[k]["side"] == "report" for k in ids):
                errors.append(path + "/sources:出入事项还须有总结报告表述，不能仅凭任务书判断未完成")
        if (review.get("findings") or any(r.get("status") == "待确认" for r in rows if isinstance(r, dict))) and not review.get("pi_questions"):
            errors.append("review/pi_questions:需要可直接发给 PI 的确认话术")
    for kind in (k for k in types if k != "review"):
        report = raw[kind]
        if not isinstance(report, dict):
            errors.append(kind + ":缺少成果报告")
            continue
        prose(report.get("value_title"), kind + "/value_title", errors, 200)
        refs(report.get("sources"), lookup, kind + "/sources", errors, "plan" if kind == "forecast" else "report")
        for i, row in enumerate(values(report.get("fact_sheet"), kind + "/fact_sheet", errors, 2, 8)):
            if not isinstance(row, dict):
                errors.append(kind + f"/fact_sheet/{i}:需要对象")
                continue
            prose(row.get("label"), kind + f"/fact_sheet/{i}/label", errors, 100)
            prose(row.get("content"), kind + f"/fact_sheet/{i}/content", errors)
        for name in ("short_version", "long_intro", "next_steps") + (("overview",) if kind == "forecast" else ()):
            paragraphs(report.get(name), kind + "/" + name, errors, maximum=4)
        for i, section in enumerate(values(report.get("long_sections"), kind + "/long_sections", errors, 3, 6)):
            if not isinstance(section, dict):
                errors.append(kind + f"/long_sections/{i}:需要小节对象")
                continue
            prose(section.get("title"), kind + f"/long_sections/{i}/title", errors, 180)
            paragraphs(section.get("paragraphs"), kind + f"/long_sections/{i}/paragraphs", errors)
        card = report.get("card")
        if not isinstance(card, dict) or set(card) != {"delivered", "method", "value", "stage", "next"}:
            errors.append(kind + "/card:需要五项可编辑卡片文字")
        else:
            for k, v in card.items():
                prose(v, kind + "/card/" + k, errors)
    for path, content in texts(raw):
        if unsupported_completion_claim(content):
            errors.append(path + ":不能扩展为正式验收或整体履约认定")
        actual_path = path.startswith("review/progress_summary/") or path.startswith(("achievement/", "forecast/"))
        if not actual_path or "/next_steps/" in path:
            continue
        current_support = "\n".join(f["text"] for f in raw["key_facts"] if isinstance(f, dict) and isinstance(f.get("text"), str)) if isinstance(raw["key_facts"], list) else ""
        for fragment in re.split(r"[。；，,]", content):
            # Dates and identifiers are not acceptance quantities. Future and
            # explicitly prior-work clauses may coexist with actual results.
            if not re.search(r"已|完成|实现|达到|超过|提升|降低", fragment) or re.search(r"计划|拟|预计|预期|后续|下一|目标|要求|前期|此前|尚未|不能", fragment):
                continue
            if types == ["forecast"]:
                if re.search(r"已(?:经|完成|实现|形成|建成|构建|发布|达到|超过|提升|降低)|目前已|现已", fragment):
                    errors.append(path + ":任务书版不能声称本项目已实现当前成果")
                continue
            metrics = re.sub(r"20\d{2}[-/年]\d{1,2}(?:[-/月]\d{1,2}[日号]?)?|20\d{2}年", "", fragment)
            if not number_support(metrics, current_support):
                missing = sorted(presentation_numbers(metrics) - presentation_numbers(current_support, chinese_counts=True))
                errors.append(path + ":实际成果数字需列入有报告来源的 key_facts：" + "、".join(missing))
    return sorted(set(errors))


def texts(value, prefix=""):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in {"sources", "id", "plans", "plan", "scope", "status", "node", "category", "label"}:
                continue
            yield from texts(v, (prefix + "/" + k).strip("/"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from texts(v, prefix + "/" + str(i))
    elif isinstance(value, str):
        yield prefix, value


def focus(raw, material, types):
    """Keep all decisions and quantitative variants; skip ordinary prose IDs."""
    groups, lookup = [], sources(material)
    plan_map = {p["id"]: p for p in raw["plans"]}

    def add(kind, paths, body, ids):
        groups.append({"id": "K" + str(len(groups) + 1), "kind": kind, "paths": paths,
                       "content": body, "sources": list(dict.fromkeys(k for k in ids if k in lookup))})

    if "review" in types:
        report = raw["review"]
        for i, row in enumerate(report["comparisons"]):
            p = plan_map[row["plan"]]
            add("计划与进展", [f"review/comparisons/{i}"], {"plan_requirement": p, **row}, p["sources"] + row.get("sources", []))
        for i, item in enumerate(report["findings"]):
            ids = item["sources"] + [s for k in item["plans"] for s in plan_map[k]["sources"]]
            add("项目出入点", [f"review/findings/{i}"], item, ids)
        add("综合意见与确认事项", ["review/conclusion", "review/pi_questions"],
            {k: report[k] for k in ("conclusion", "pi_questions")},
            [k for p in raw["plans"] for k in p["sources"]] + [k for f in raw["key_facts"] for k in f["sources"]])
    if "review" not in types:
        add("目标与交付条件", ["plans"], raw["plans"], [k for p in raw["plans"] for k in p["sources"]])
    for i, fact in enumerate(raw["key_facts"]):
        add("预期指标" if types == ["forecast"] else "关键事实", [f"key_facts/{i}"], fact["text"], fact["sources"])
    variants = {}
    for path, text in texts({k: raw[k] for k in types}):
        if "/comparisons/" in path or "/findings/" in path or path.startswith("review/conclusion"):
            continue
        numbers = tuple(sorted(presentation_numbers(text)))
        strong = bool(re.search(r"首个|首次|首创|领先|已部署|已上线|全面完成|正式验收", text))
        if numbers or strong:
            variants.setdefault((numbers, strong), []).append({"path": path, "text": text})
    for rows in variants.values():
        ids = [k for f in raw["key_facts"] for k in f["sources"]]
        ids += [k for p in raw["plans"] for k in p["sources"]]
        ids += [s for k in types if k != "review" for s in raw[k]["sources"]]
        add("重要数字与表述（所有变体均需阅读）", [r["path"] for r in rows], rows, ids)
    return groups


def check(raw, groups):
    if not isinstance(raw, dict):
        return ["check:需要重点核对结果对象"]
    expected = {g["id"] for g in groups}
    reviewed = raw.get("reviewed")
    if not isinstance(reviewed, list) or any(not isinstance(k, str) for k in reviewed) or len(reviewed) != len(set(reviewed)) or set(reviewed) != expected:
        return ["check/reviewed:须列出本次所有 K 编号，只需短编号数组，不逐条填写通过布尔值"]
    issues = raw.get("issues")
    if not isinstance(issues, list):
        return ["check/issues:须返回实质问题数组，无问题填空数组"]
    errors = []
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("path"), str) or not isinstance(issue.get("reason"), str) or not issue["reason"].strip():
            errors.append("check/issues:每个问题须有具体 path 和 reason")
        else:
            errors.append(issue["path"].lstrip("/") + ":" + issue["reason"][:1000])
    return errors


def patch_roots(errors):
    roots = set()
    for error in errors:
        parts = error.partition(":")[0].strip("/").split("/")
        if parts[0] == "compose":
            roots.update({"/plans", "/key_facts", "/review", "/achievement", "/forecast"})
        elif parts[0] == "plans":
            roots.add("/plans")
        elif parts[0] == "key_facts":
            roots.add("/" + "/".join(parts[:2]))
        elif parts[0] in {"review", "achievement", "forecast"}:
            depth = 3 if len(parts) > 2 and parts[2].isdigit() else 2
            roots.add("/" + "/".join(parts[:depth]))
        if "实际成果数字" in error:
            roots.add("/key_facts")
    return [root for root in sorted(roots) if not any(root.startswith(parent + "/") for parent in roots if parent != root)]


def apply_patches(original, patches, roots):
    if not isinstance(patches, list) or not patches or len(patches) > 100:
        raise ValueError("修订只提交非空 patches 数组，不重写整份报告")
    result, used = copy.deepcopy(original), []
    for item in patches:
        if not isinstance(item, dict) or set(item) != {"path", "value"} or not isinstance(item["path"], str):
            raise ValueError("每个修订项只包含 path 和 value")
        path = item["path"]
        if not path.startswith("/") or not any(path == root or path.startswith(root + "/") for root in roots):
            raise ValueError("修订路径不属于本次反馈，不能修改无关内容")
        if any(path == other or path.startswith(other + "/") or other.startswith(path + "/") for other in used):
            raise ValueError("修订路径重复或互相包含，请合并后提交")
        used.append(path)
        keys = [k.replace("~1", "/").replace("~0", "~") for k in path[1:].split("/")]
        target = result
        try:
            for key in keys[:-1]:
                target = target[int(key)] if isinstance(target, list) and key.isdigit() else target[key]
            key = keys[-1]
            if isinstance(target, list) and key.isdigit() and int(key) < len(target):
                target[int(key)] = item["value"]
            elif isinstance(target, dict):
                target[key] = item["value"]
            else:
                raise ValueError("修订路径不存在")
        except (KeyError, IndexError, TypeError):
            raise ValueError("修订路径不存在，请使用反馈给出的 JSON 路径") from None
    return result


def canonical(raw, material, types):
    """Adapt plain paragraphs to the existing layout without rewriting prose."""
    def unit(text):
        return {"text": text}

    plans = {p["id"]: p for p in raw["plans"]}
    facts = {"plan_items": [{"item_id": p["id"], **p} for p in plans.values()]}
    draft, claims, lookup = {}, [], sources(material)

    def claim(path, text, ids):
        claims.append({"path": path, "text": text, "sources": [lookup[k] for k in dict.fromkeys(ids)]})

    if "review" in types:
        r = raw["review"]
        draft["review"] = {k: [unit(t) for t in r[k]] for k in ("overview", "plan_summary", "progress_summary", "conclusion", "pi_questions", "manager_notes")}
        draft["review"]["comparisons"] = [{"item_id": row["plan"], **row} for row in r["comparisons"]]
        findings = []
        for i, f in enumerate(r["findings"]):
            requirement = "；".join(plans[k]["requirement"] for k in f["plans"])
            ids = f["sources"] + [s for k in f["plans"] for s in plans[k]["sources"]]
            value = {"category": f["category"], "title": unit(f["title"]), "requirement": unit(requirement),
                     "progress": {"text": f["report"], "kind": "材料表述"}, "difference": unit(f["difference"])}
            if all(plans[k]["scope"] in {"总体", "后续"} for k in f["plans"]):
                value["scope_note"] = "总体或后续目标进展参考"
            findings.append(value)
            claim(f"review/findings/{i}", f["title"], ids)
        draft["review"]["findings"] = findings
        claim("review/plan_summary", "任务书目标与阶段计划", [k for p in plans.values() for k in p["sources"]])
        claim("review/progress_summary", "阶段进展与代表成果", [k for f in raw["key_facts"] for k in f["sources"]])
    for kind in (k for k in types if k != "review"):
        r = raw[kind]
        value = {"value_title": unit(r["value_title"]), "fact_sheet": [{"label": f["label"], "content": unit(f["content"])} for f in r["fact_sheet"]],
                 "long_sections": [{"title": unit(s["title"]), "paragraphs": [unit(p) for p in s["paragraphs"]]} for s in r["long_sections"]],
                 "card": {k: unit(v) for k, v in r["card"].items()}}
        for field in ("short_version", "long_intro", "next_steps") + (("overview",) if kind == "forecast" else ()):
            value[field] = [unit(p) for p in r[field]]
        draft[kind] = value
        claim(kind + "/plan_summary", "任务书目标与应用背景", [k for p in plans.values() for k in p["sources"]])
        claim(kind + "/progress_summary", "预期成果与用途" if kind == "forecast" else "代表成果、关键指标与用途", r["sources"])
    return draft, facts, claims
