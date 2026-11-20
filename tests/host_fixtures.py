"""Synthetic project and host replies; no project files or paid model calls."""

from io import BytesIO
from docx import Document

PLAN = "2026年5月31日前完成32B模型概念验证并发布初版模型。"
FUTURE = "2027年12月31日前构建300项评测任务。"
ACTUAL = "已完成8B模型验证。"
UNFINISHED = "目标模型尚在训练中，初版模型尚未发布。"
NEXT = "计划下一阶段开展32B模型验证。"


def documents():
    plan = Document()
    for line in ("项目名称：模型训练验证研究", "项目承担单位：样例研究单位", "项目负责人：某教授",
                 "六、项目的里程碑、年度计划及年度目标", PLAN, FUTURE):
        plan.add_paragraph(line)
    report = Document()
    for line in ("二、项目实施进展", ACTUAL, UNFINISHED, "三、总结及下一步计划", NEXT):
        report.add_paragraph(line)
    output = []
    for value in (plan, report):
        stream = BytesIO()
        value.save(stream)
        output.append(stream.getvalue())
    return output


def fact_reply(request):
    sources = request["sources"]
    plans, facts, coverage = [], [], []
    for source in sources:
        value = source["text"]
        cite = [{"source_id": source["source_id"], "quote": value}]
        requirement = source["side"] == "plan" and value in (PLAN, FUTURE)
        if requirement:
            plans.append({"title": "模型验证与发布" if value == PLAN else "评测任务建设", "requirement": value,
                          "node": "2026年5月31日" if value == PLAN else "2027年12月31日",
                          "scope": "本期" if value == PLAN else "后续", "citations": cite})
        if source["side"] == "report" and value in (ACTUAL, UNFINISHED, NEXT):
            facts.append({"text": value, "kind": "后续计划" if value == NEXT else "阶段进展", "citations": cite})
        if source["plan_candidate"]:
            coverage.append({"source_id": source["source_id"], "disposition": "requirements" if requirement else "not_requirement",
                             "reason": "" if requirement else "章节标题或背景说明"})
    return {"plan_items": plans, "facts": facts, "coverage": coverage}


def draft_reply(request):
    facts = request["facts"]
    p = [item["item_id"] for item in facts["plan_items"] if item["scope"] == "本期"]
    future = [item["item_id"] for item in facts["plan_items"] if item["scope"] == "后续"]
    f = [item["fact_id"] for item in facts["facts"] if item["kind"] == "阶段进展"]

    def unit(text, kind="分析", plans=None, actual=None):
        return {"text": text, "kind": kind, "plan_ids": p if plans is None else plans,
                "fact_ids": f if actual is None else actual}

    current = unit(ACTUAL + UNFINISHED, "事实", [], f)
    goal = unit(PLAN, "计划", p, [])
    comparison = unit("现阶段已完成小规模模型验证，任务书约定的目标模型验证和发布仍需衔接。")
    question = unit("想和您确认一下，报告中的8B验证是否属于过渡验证？任务书要求的32B验证和初版发布目前进展如何？", "建议")
    result = {}
    if "review" in request["requested_types"]:
        result["review"] = {
            "overview": [unit("项目围绕模型训练验证推进，计划形成目标模型的概念验证和初版模型。当前已有小规模验证进展。")],
            "plan_summary": [goal], "progress_summary": [current],
            "comparisons": [
                {"item_id": p[0], "fact_ids": f, "status": "部分完成", "progress": ACTUAL + UNFINISHED,
                 "judgement": comparison["text"]},
                *[{"item_id": key, "fact_ids": [], "status": "后续节点", "progress": "", "judgement": "该项为后续计划，当前仅列作参考。"} for key in future]],
            "findings": [{"category": "交付内容与技术规格差异", "title": unit("目标模型验证与发布状态"),
                          "requirement": goal, "progress": current, "difference": comparison}],
            "conclusion": [comparison, unit("建议重点跟进目标模型验证及初版发布的衔接安排。", "建议")],
            "pi_questions": [question], "manager_notes": []}
    for kind in [k for k in request["requested_types"] if k != "review"]:
        forecast = kind == "forecast"
        body = goal if forecast else current
        title = unit("让模型验证逐步走向目标规模", "计划" if forecast else "分析", p, [] if forecast else f)
        value = unit("计划形成目标模型的概念验证和初版模型，为后续评测提供基础。" if forecast else comparison["text"],
                     "计划" if forecast else "分析", p, [] if forecast else f)
        next_step = unit("后续结合阶段总结和验证材料，核对目标模型验证与初版发布的进展。", "建议", p, [] if forecast else f)
        expression = {
            "value_title": title,
            "fact_sheet": [{"label": "形成什么", "content": body}, {"label": "有什么用", "content": value}],
            "short_version": [body, value], "long_intro": [value],
            "long_sections": [{"title": unit(label, "计划" if forecast else "分析", p, [] if forecast else f),
                               "paragraphs": [body, value]} for label in ("先验证什么", "模型发布如何衔接", "后续评测如何开展")],
            "next_steps": [next_step],
            "card": {"delivered": body, "method": value, "value": value, "stage": value, "next": next_step}}
        if forecast:
            expression["overview"] = [value]
        result[kind] = expression
    return result


def check_reply(request):
    if "groups" in request:
        return {"reviewed": [g["id"] for g in request["groups"]], "issues": [], "editorial_notes": []}
    return {"checks": [{"claim_id": claim["claim_id"], "supported": True, "reason": ""} for claim in request["claims"]],
            "plan_coverage_ok": True, "stage_scope_ok": True, "readable_and_complete": True,
            "cross_report_consistent": True, "issues": []}


def reply(request):
    if request["stage"] in {"compose", "repair"}:
        from pathlib import Path
        import json
        from host_reports import source_context, at
        context = request
        if request["stage"] == "repair":
            folder = Path(request["response_file"]).parent
            material = json.loads((folder / "materials.json").read_text())
            context = {**request, "source_context": source_context(material)}
        result = compose_reply(context)
        if request["stage"] == "repair":
            return {"request_id": request["request_id"], "patches": [
                {"path": path, "value": at(result, path)} for path in request["allowed_roots"]]}
        return {"request_id": request["request_id"], "result": result}
    return {"request_id": request["request_id"],
            "result": {"facts": fact_reply, "draft": draft_reply, "check": check_reply}[request["stage"]](request)}


def compose_reply(request):
    by_text = {s["text"]: s["id"] for s in request["source_context"]}
    forecast = request["requested_types"] == ["forecast"]
    plans = [{"id": "P1", "title": "模型验证与发布", "requirement": PLAN, "node": "2026年5月31日",
              "scope": "本期", "sources": [by_text[PLAN]]},
             {"id": "P2", "title": "评测任务建设", "requirement": FUTURE, "node": "2027年12月31日",
              "scope": "后续", "sources": [by_text[FUTURE]]}]
    current = PLAN if forecast else ACTUAL + UNFINISHED
    ids = [by_text[PLAN]] if forecast else [by_text[ACTUAL], by_text[UNFINISHED]]
    result = {"plans": plans, "key_facts": [{"text": current, "sources": ids}]}
    if "review" in request["requested_types"]:
        result["review"] = {
            "overview": ["项目围绕模型训练验证推进，计划形成目标模型的概念验证和初版模型。当前已有小规模验证进展。"],
            "plan_summary": [PLAN], "progress_summary": [current],
            "comparisons": [{"plan": "P1", "status": "部分完成", "progress": current, "sources": ids,
                             "judgement": "现阶段已有小规模验证进展，目标模型验证和发布仍需衔接。"},
                            {"plan": "P2", "status": "后续节点", "progress": "", "sources": [],
                             "judgement": "该项属于后续计划，当前列作参考。"}],
            "findings": [{"category": "交付内容与技术规格差异", "title": "目标模型验证与发布状态",
                          "plans": ["P1"], "report": current, "difference": "任务书目标模型与当前验证规模存在出入。",
                          "sources": ids}],
            "conclusion": ["项目已形成小规模验证进展，目标模型验证和初版发布仍需跟进。"],
            "pi_questions": ["想和您确认一下，报告中的8B验证是否为过渡验证？任务书要求的32B验证和初版发布目前进展如何？"],
            "manager_notes": []}
    for kind in [t for t in request["requested_types"] if t != "review"]:
        value = "计划形成目标模型的概念验证和初版模型，为后续评测提供基础。" if forecast else "已形成小规模模型验证进展，为后续目标模型验证提供基础。"
        next_step = "后续结合阶段总结和验证材料，核对目标模型验证与初版发布的进展。"
        result[kind] = {
            "value_title": "让模型验证逐步走向目标规模", "sources": ids,
            "fact_sheet": [{"label": "形成什么", "content": current}, {"label": "有什么用", "content": value}],
            "short_version": [current, value], "long_intro": [value],
            "long_sections": [{"title": label, "paragraphs": [current, value]} for label in
                              ("先验证什么", "模型发布如何衔接", "后续评测如何开展")],
            "next_steps": [next_step], "card": {"delivered": current, "method": value, "value": value,
                                              "stage": value, "next": next_step}}
        if forecast:
            result[kind]["overview"] = [value]
    return result
