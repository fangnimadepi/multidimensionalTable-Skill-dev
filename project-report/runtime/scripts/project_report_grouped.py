"""Grouped report composition using the existing independent business validators.

The temporary candidate is never published. Compact audit tuples are expanded
only from explicit model verdicts; missing/duplicate coverage is a hard error.
"""

from __future__ import annotations
import copy
import datetime as dt
import re
import time
from collections import Counter

import project_report_contract as c
import project_report_presentation as p
import project_report_assurance as a
import project_report_coordination as co
import project_report_synthesis as sy
import project_report_followup as fu
import project_report_evidence as ev
import project_report_prompts as prompts
from llm_runtime import parallel_map

VERSION = "grouped-composition-v1"
EVIDENCE_FIELDS = (
    "excerpt_complete",
    "explanation_supported",
    "context_preserved",
    "conflict_preserved",
)
QUESTION_FIELDS = (
    "supported",
    "questions_preserved",
    "no_new_requests",
    "audience_correct",
    "no_new_claims",
)
MODULE_FIELDS = (
    "reader_friendly",
    "not_repetitive",
    "scope_preserved",
    "states_consistent",
)


def checks(rows, expected, field, flags=("supported",)):
    if not isinstance(rows, list) or Counter(
        row[0] for row in rows if isinstance(row, list) and row
    ) != Counter(expected):
        raise ValueError("Grouped review audit coverage incomplete: " + field)
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) not in (2, 3):
            raise ValueError("Grouped review audit shape invalid")
        values = row[1] if isinstance(row[1], list) else [row[1]]
        if len(values) != len(flags) or any(
            type(value) is not bool for value in values
        ):
            raise ValueError("Grouped review audit boolean missing: " + field)
        if not all(values) and (
            len(row) != 3 or not isinstance(row[2], str) or not row[2].strip()
        ):
            raise ValueError("Grouped review rejected item lacks reason: " + field)
        result.append(
            {
                field: row[0],
                **dict(zip(flags, values)),
                "reason": row[2] if len(row) == 3 else "联合独立复核标记通过。",
            }
        )
    return result


def complete_missing_pair_check(call, stage, answer, paired):
    """Recover an omitted protocol field without changing any audit verdict."""
    if paired is None or "paired_check" in answer:
        return answer, stage
    from achievement_expression_prompts import PAIRED

    repair_stage = stage + "-missing-pair-check"
    supplemental = call(
        repair_stage,
        PAIRED + '\n待比较的原文位于extra_modules.paired_reports。独立检查这两份候选，不改写正文。'
        '只返回JSON {"paired_check":{"consistent":true,"issues":[]}}；有实质矛盾或不能确认时consistent为false并列出具体原因。',
        {"extra_modules": {"paired_reports": paired}},
        1200,
    )
    return {**answer, "paired_check": supplemental.get("paired_check")}, repair_stage


INITIAL_AUDIT_HEAD = (
    prompts.READER_AUDIT_PROMPT + "\n" + prompts.ACCEPTANCE_AUDIT_PROMPT + """
本轮合并两种独立检查，不改写任何草稿。材料已确认，不调查真实性。shared_sources提供共同原文；reader_context与basis_context提供各自的范围和已审定逐项判断。
必须明确区分专利交底书、申请提交、受理，论文投稿、录用、发表，评测集、训练集，以及局部验证、整项完成。报告中的另一处已完成/仍推进冲突必须保留。
"""
)
MEASUREMENT_AUDIT = "核对实际进展时，把同一实验的测试时长、数据范围、基线与指标作为一个整体对照原文。数字分别出现在不同实验中不等于它们可组合。条件与结果配错必须判false，并指出原文中的对应关系。报告自身有两种口径时，不能合并成一个结果。\n"
INITIAL_AUDIT_TAIL = """\
每个reader_draft中的display_id、每个acceptance_draft中的check_id各检查一次，未知编号或漏项均禁止。true表示按上述完整条件支持；false必须给简短实质理由。不重抄文字。
只输出JSON {"reader":[["S01",true]],"basis":[["A01",true]]}；拒绝例["S02",false,"当前事实没有支持该结论"]。没有相应项目的数组填[]。
"""
INITIAL_AUDIT = INITIAL_AUDIT_HEAD + INITIAL_AUDIT_TAIL

FINAL_WRITE = prompts.SYNTHESIS_PROMPT + "\n" + prompts.CONFIRMATION_PROMPT + """
本轮一次完成综合意见和集中确认话术。conclusion_context中保留全部已审定问题供理解，但综合意见只点出key_pending_issue_ids中的一个主要待确认主题，其他问题完整留在questions，不在综合意见展开清单。
综合意见保持自然、有观点：先概括实际进展，再解释其与计划的对应程度及一个关键边界；不用机械数字堆砌，也不能把未确定事项当作全部满足。
confirmation_context.approved_questions每个entry_id恰好一次。综合意见的sources与问题原文分别属于对应上下文，不能混用编号或引入新要求。
只输出JSON {"conclusion":{"paragraphs":[{"text":"自然的一至两段综合意见","has_assessment":true,"item_ids":["C001"],"issue_ids":[],"source_ids":["依据01","依据02"]}]},"questions":{"items":[{"entry_id":"Q01","text":"想和您确认一下，……目前进展如何？"}]}}。
"""
FINAL_AUDIT = (
    prompts.SYNTHESIS_AUDIT_PROMPT
    + "\n"
    + prompts.CONFIRMATION_AUDIT_PROMPT
    + "\n"
    + prompts.EVIDENCE_CONTEXT_AUDIT_PROMPT
    + """
本轮合并最终文字、问题与原文用途的独立审核，不能自行改写、替换或覆盖任何负向结果。extra_modules非空时，还须检查综合意见、已认定成果状态和所有确认话术；为空时仅检查本批证据。
保留上述各项语义边界，但按紧凑协议输出，不抄写合格原文。JSON:
{"evidence":[{"id":"依据01","flags":[true,true,true,true],"ok_uses":["U001"],"issues":[]}],
"conclusion":[["CS01",true]],"module":[true,true,true,true],"states":[["C001",true,true]],
"questions":[["Q01",[true,true,true,true,true]]]}
evidence flags依次为excerpt_complete/explanation_supported/context_preserved/conflict_preserved；四项必须显式布尔值。ok_uses列出逐项支持的本卡uses.use_id，issues为[{"use_id":"U002","reason":"错误原因"}]，两者必须不重复且完整覆盖本卡uses，不能因少输出而跳过。flags任何一项false时在本卡额外返回reason字符串；不能只返回false而没有具体理由。
conclusion逐段返回paragraph_id/支持与否；module依次reader_friendly/not_repetitive/scope_preserved/states_consistent；states为每项已认定成果的item_id/是否提及/状态是否一致；questions五项布尔值依次supported/questions_preserved/no_new_requests/audience_correct/no_new_claims。所有拒绝都需第三项简短原因。
extra_modules为空时conclusion/states/questions返回[]、module返回[]。不得直接批准草稿，来源只支持自身对多来源结论的贡献，不要求单段承担整个对照；未来安排不支撑当前完成，冲突不能升级为未达。
"""
)


def evidence_audit(answer, groups):
    rows = answer.get("evidence")
    if not isinstance(rows, list) or Counter(
        row.get("id") for row in rows if isinstance(row, dict)
    ) != Counter(g["source_id"] for g in groups):
        raise ValueError("Grouped review evidence source coverage incomplete")
    lookup = {g["source_id"]: g for g in groups}
    out = []
    for row in rows:
        flags = row.get("flags")
        ok = row.get("ok_uses")
        bad = row.get("issues")
        if (
            not isinstance(flags, list)
            or len(flags) != 4
            or any(type(v) is not bool for v in flags)
            or not isinstance(ok, list)
            or not isinstance(bad, list)
        ):
            raise ValueError("Grouped review evidence flags incomplete")
        if any(not isinstance(v, str) for v in ok) or any(
            not isinstance(v, dict)
            or not isinstance(v.get("reason"), str)
            or not v["reason"].strip()
            for v in bad
        ):
            raise ValueError("Grouped review evidence verdict invalid")
        ids = ok + [v.get("use_id") for v in bad]
        if Counter(ids) != Counter(v["use_id"] for v in lookup[row["id"]]["uses"]):
            raise ValueError("Grouped review evidence usage coverage incomplete")
        if not all(flags) and not row.get("reason"):
            raise ValueError("Grouped review rejected source lacks reason")
        out.append(
            {
                "source_id": row["id"],
                **dict(zip(EVIDENCE_FIELDS, flags)),
                "reason": row.get("reason") or "联合独立复核标记通过。",
                "use_checks": [{"use_id": v, "supported": True} for v in ok]
                + [
                    {"use_id": v["use_id"], "supported": False, "reason": v["reason"]}
                    for v in bad
                ],
            }
        )
    return {"checks": out}


def call_adapter(pipeline, report_id, config, execute, rebuild_only, calls):
    def call(stage, system, payload, tokens):
        answer, usage = pipeline.cached_call(
            report_id, stage, system, payload, config, execute, rebuild_only, tokens
        )
        calls.append({"stage": stage, **usage})
        return answer

    return call


def complete_initial_audit(call, audit, payload, stage, prompt=INITIAL_AUDIT):
    """Supplement only omitted IDs; never overwrite an explicit rejection."""
    missing = {}
    reader = payload["reader_draft"]
    expected_reader = [
        v["display_id"]
        for field in ("statements", "confirmation_notes", "follow_up_plans")
        for v in reader.get(field, [])
    ]
    expected_basis = [
        v["check_id"] for v in payload["acceptance_draft"].get("items", [])
    ]
    for name, ids in (("reader", expected_reader), ("basis", expected_basis)):
        rows = audit.get(name)
        if not isinstance(rows, list):
            rows = []
        seen = []
        for row in rows:
            if not isinstance(row, list) or not row:
                raise ValueError("Grouped review audit row malformed")
            checks([row], [row[0]], "display_id" if name == "reader" else "check_id")
            seen.append(row[0])
        if len(seen) != len(set(seen)) or set(seen) - set(ids):
            raise ValueError("Grouped review audit unknown or duplicate IDs")
        audit[name] = rows
        missing[name] = set(ids) - set(seen)
    if not any(missing.values()):
        return audit
    supplement = copy.deepcopy(payload)
    supplement["reader_draft"] = {
        field: [
            v for v in reader.get(field, []) if v["display_id"] in missing["reader"]
        ]
        for field in ("statements", "confirmation_notes", "follow_up_plans")
    }
    supplement["acceptance_draft"] = {
        "items": [
            v
            for v in payload["acceptance_draft"]["items"]
            if v["check_id"] in missing["basis"]
        ]
    }
    supplement["instruction"] = (
        "上次审核遗漏这些ID，本次只补审给出的项目，不能修改或重新批准其他已检查项目。"
    )
    answer = call(stage + "-coverage", prompt, supplement, 1600)
    for name in ("reader", "basis"):
        rows = answer.get(name, [])
        checks(
            rows, list(missing[name]), "display_id" if name == "reader" else "check_id"
        )
        audit[name] += rows
    return audit


def initial(
    pipeline,
    pair,
    source,
    records,
    items,
    rows,
    inventory,
    errors,
    metadata,
    config,
    execute,
    rebuild_only,
    calls,
):
    call = call_adapter(pipeline, pair.project_id, config, execute, rebuild_only, calls)
    reader_context = p.presentation_context(source, records, items, rows)
    measured_duration = any(
        re.search(r'\d+(?:\.\d+)?\s*(?:ms\b|毫秒)', record["text"], re.IGNORECASE)
        for record in records.values()
        if record.get("role") in {"actual_or_body", "summary_and_next"}
    )
    audit_prompt = INITIAL_AUDIT_HEAD + MEASUREMENT_AUDIT + INITIAL_AUDIT_TAIL if measured_duration else INITIAL_AUDIT
    skeleton = c.assemble_report(
        source, records, items, rows, inventory, errors, [], [], metadata
    )
    basis_context = a.acceptance_context(skeleton, source, records)
    drafts = {}
    approved = {}
    feedback = {}
    for attempt in range(2):
        pending = [kind for kind in ("reader", "basis") if kind not in approved]
        bound = (
            repair_numeric_sources(call, drafts, feedback, records)
            if attempt
            else set()
        )

        def write(kind):
            if kind in bound:
                return kind, drafts[kind]
            context = reader_context if kind == "reader" else basis_context
            if kind == "basis" and not context["plan_items"]:
                return kind, {"items": [], "summary": None}
            system = (
                prompts.CORE_READER_PROMPT
                + "\n本次只生成overview/plan_summary/achievements三段；confirmation_notes与follow_up_plans均填[]，业务确认问题由程序原样引用已独立审定的对照记录，不在此新增。"
                + ("\n写实际进展中的量化结果时，测试时长、数据范围、对比基线与误差值必须来自同一实验的同一表述。不要把相邻实验的时间条件拼到另一项指标上。原文有不同口径或不容易逐一核实时，分别说明或保留可证实的定性进展。修订时优先纠正审查指出的条件和结果对应关系。" if measured_duration else "")
                if kind == "reader"
                else prompts.ACCEPTANCE_BASIS_PROMPT
                + "\nsummary必须填null；本次不写认定综述，统一由最终综合意见表达。"
            )
            payload = {
                **context,
                **(
                    {
                        "editing_feedback": feedback.get(kind),
                        "previous_draft": drafts.get(kind),
                    }
                    if attempt
                    else {}
                ),
            }
            raw = call(
                "grouped-" + kind + "-write" + ("-repair" if attempt else ""),
                system,
                payload,
                4200 if kind == "reader" else 2400,
            )
            if kind == "reader":
                raw["confirmation_notes"] = checked_questions(items, rows)
                raw["follow_up_plans"] = []
                raw = p.resolve_reader_sources(
                    p.assign_display_ids(raw), source, records
                )
                raw["statements"] = [
                    value
                    for value in raw.get("statements", [])
                    if value.get("section") != "conclusion"
                ]
            else:
                raw["summary"] = None
                raw = a.resolve(raw, source, records, "items", "A", with_summary=True)
            return kind, raw

        drafts.update(dict(parallel_map(write, pending, 2)))
        stage = "grouped-initial-audit" + ("-repair" if attempt else "")
        audit_payload = {
            "shared_sources": {"plan": source["plan_blocks"], "report": records},
            "reader_context": {
                k: v
                for k, v in reader_context.items()
                if k not in {"plan_sources", "report_sources"}
            },
            "basis_context": {
                k: v
                for k, v in basis_context.items()
                if k not in {"plan_sources", "report_sources"}
            },
            "reader_draft": drafts["reader"] if "reader" in pending else {},
            "acceptance_draft": (
                drafts["basis"] if "basis" in pending else {"items": []}
            ),
            "deliverable_state_checks": (
                a.deliverable_state_context(drafts["basis"], records, items)
                if "basis" in pending
                else []
            ),
        }
        audit = call(stage, audit_prompt, audit_payload, 2400)
        audit = complete_initial_audit(call, audit, audit_payload, stage, audit_prompt)
        if "reader" in pending:
            ids = [
                v["display_id"]
                for field in ("statements", "confirmation_notes", "follow_up_plans")
                for v in drafts["reader"].get(field, [])
            ]
            checked, rejected = p.validate_presentation(
                drafts["reader"],
                {"checks": checks(audit.get("reader"), ids, "display_id")},
                source,
                records,
                items,
                rows,
                required_sections=p.CORE_SECTIONS,
            )
            if (
                not checked["missing_sections"]
                and not checked["uncovered_business_items"]
                and not rejected
            ):
                approved["reader"] = checked
                feedback.pop("reader", None)
            else:
                feedback["reader"] = {
                    "missing": checked["missing_sections"],
                    "uncovered": checked["uncovered_business_items"],
                    "rejected": rejected,
                }
        if "basis" in pending:
            ids = [v["check_id"] for v in drafts["basis"]["items"]]
            checked, rejected = a.validate_acceptance(
                drafts["basis"],
                {"checks": checks(audit.get("basis"), ids, "check_id")},
                basis_context,
                source,
                records,
                include_summary=False,
            )
            if not checked["missing_item_ids"] and not rejected:
                approved["basis"] = checked
                feedback.pop("basis", None)
            else:
                feedback["basis"] = {
                    "missing": checked["missing_item_ids"],
                    "rejected": rejected,
                }
        if set(approved) == {"reader", "basis"}:
            break
    if set(approved) != {"reader", "basis"}:
        pipeline.write_json(
            pipeline.RUN / "failed-grouped" / f"{pair.project_id}-initial.json",
            feedback,
        )
        raise ValueError("Grouped review initial composition did not pass checks")
    report = c.assemble_report(
        source,
        records,
        items,
        rows,
        inventory,
        errors,
        approved["reader"]["statements"],
        [],
        metadata,
    )
    p.attach_presentation(report, approved["reader"], [])
    basis = a.retain_uncertain_acceptance(report, basis_context, approved["basis"])
    a.attach(
        report,
        basis,
        {"version": a.VERSION, "items": [], "generation_status": "not_requested"},
        [],
    )
    co.attach_checked_topics(report, source, records)
    return report


def final(
    pipeline,
    report,
    source,
    records,
    config,
    execute,
    rebuild_only,
    calls,
    paired_provider=None,
):
    call = call_adapter(
        pipeline, report["project_id"], config, execute, rebuild_only, calls
    )
    context = sy.context_for(report, source, records)
    qcontext, targets = fu.context_for(report)
    key_issues = context["pending_questions"][:1]
    paired = None
    feedback = []
    for attempt in range(2):
        payload = {
            "conclusion_context": context,
            "confirmation_context": qcontext,
            "key_pending_issue_ids": [
                key for value in key_issues for key in value["item_ids"]
            ],
        }
        if attempt:
            payload.update(
                previous_draft=raw,
                editing_feedback=feedback,
                instruction="只修订反馈指出的部分，保留其余合格文字、来源和范围；两部分仍完整返回。",
            )
            numeric_errors = {error.split(":", 1)[1] for error in feedback.get("conclusion", [])
                              if error.startswith("unsupported_conclusion_number:")} if isinstance(feedback, dict) else set()
            if numeric_errors:
                payload["numeric_binding_feedback"] = sy.numeric_binding_feedback(conclusion, context, numeric_errors)
                payload["instruction"] += (
                    "数字修订：对每段的缺失数字逐项补选同对象、同口径、同阶段的原文来源，"
                    "或删去缺少依据的数字及相应断言。候选编号只是检索线索，数字相同但含义不同时不能引用。"
                    "计划数字不能支撑实际完成情况。不要原样返回含缺失数字的段落。"
                )
        raw = call(
            "grouped-final-write" + ("-repair" if attempt else ""),
            FINAL_WRITE,
            payload,
            4000,
        )
        conclusion = sy.resolve(raw.get("conclusion", {}), context)
        conclusion = bind_assessment_plan_sources(conclusion, context)
        questions = fu.resolve(raw.get("questions", {}), targets)
        if Counter(v.get("entry_id") for v in questions.get("items", [])) != Counter(
            t["entry_id"] for t in targets
        ):
            feedback = [{"questions": "missing_or_duplicate_entry_id"}]
            continue
        candidate = copy.deepcopy(report)
        sy.attach(
            candidate,
            {
                "version": sy.VERSION,
                "paragraphs": conclusion.get("paragraphs", []),
                "semantic_reviewed": False,
                "audit": {},
            },
            [],
        )
        question_map = {v["entry_id"]: v["text"] for v in questions["items"]}
        fu.attach(
            candidate,
            {
                "version": fu.VERSION,
                "items": [
                    {
                        **copy.deepcopy(t),
                        "text": question_map[t["entry_id"]],
                        "semantic_reviewed": False,
                    }
                    for t in targets
                ],
                "missing_entry_ids": [],
            },
            [],
        )
        groups, uses = ev.context_for(candidate, source, records)
        literal = ev.resolve({"items": [ev.literal_card(g) for g in groups]}, groups)
        drafts = {v["source_id"]: v for v in literal["items"]}
        paired_text = None
        # Conclusion writing depends on the reviewed fulfillment records only.
        # Wait for the sibling narrative just before the joint audit so its
        # remaining generation can overlap with this independent writing step.
        if paired is None and paired_provider:
            paired = paired_provider()
        if paired:
            # Only render the temporary candidate. No audit or publication flag
            # is set until the independent response passes every content gate.
            candidate["presentation"]["evidence_notes"] = {
                "version": ev.VERSION,
                "uses": uses,
                "items": [
                    {
                        **copy.deepcopy(group),
                        **drafts[group["source_id"]],
                        "use_ids": [value["use_id"] for value in group["uses"]],
                        "semantic_reviewed": False,
                    }
                    for group in groups
                ],
            }
            for card in candidate["presentation"]["evidence_notes"]["items"]:
                blocks = {
                    value["context_id"]: value for value in card["context_blocks"]
                }
                for extra in card.get("context_excerpts", []):
                    block = blocks[extra["context_id"]]
                    extra.update(
                        ref=block["ref"], label=block["label"], role=block["role"]
                    )
            paired_text = c.render_markdown(candidate).split("## 附录：", 1)[0]
        batches = list(
            ev.literal_audit_batches(groups, drafts, limit=8, characters=52000, uses=48)
        ) or [[]]

        def audit_batch(entry):
            index, chunk = entry
            extra = (
                {
                    "conclusion_context": context,
                    "synthesis_draft": conclusion,
                    "confirmation_context": qcontext,
                    "question_draft": questions,
                }
                if index == 0
                else {}
            )
            if index == 0 and paired:
                extra["paired_reports"] = {**paired, "paired_review": paired_text}
            audit_prompt = FINAL_AUDIT
            if index == 0 and paired:
                from achievement_expression_prompts import PAIRED

                audit_prompt += (
                    "\n"
                    + PAIRED
                    + '\n本次paired_reports给出两份最终候选正文，按上述规则独立检查，输出额外字段paired_check:{"consistent":true,"issues":[]}。不要重写文章。'
                )
            answer = call(
                f"grouped-final-audit-{index+1:02d}" + ("-repair" if attempt else ""),
                audit_prompt,
                {**ev.audit_context(chunk, groups, drafts), "extra_modules": extra,
                 "expected_response_ids": {
                     "evidence": [g["source_id"] for g in chunk],
                     "conclusion": [v["paragraph_id"] for v in conclusion.get("paragraphs", [])] if extra else [],
                     "states": [v["item_id"] for v in context["established_reported_results"]] if extra else [],
                     "questions": [t["entry_id"] for t in targets] if extra else [],
                 },
                 "response_scope": "各数组仅审核expected_response_ids对应编号，恰好一次；states只核对已认定成果，不扩展为全部计划或其他模块的同名编号。"},
                3000 if index == 0 else 2400,
            )
            audit = evidence_audit(answer, chunk)
            checked, rejected = ev.validate(
                {"items": [drafts[g["source_id"]] for g in chunk]}, audit, chunk, drafts
            )
            return answer, checked, rejected

        results = parallel_map(
            audit_batch, list(enumerate(batches)), config.get("audit_workers", 2)
        )
        first = results[0][0]
        paired_stage = "grouped-final-audit-01" + ("-repair" if attempt else "")
        first, paired_stage = complete_missing_pair_check(
            call, paired_stage, first,
            {**paired, "paired_review": paired_text} if paired else None,
        )
        paired_audit = first.get("paired_check") if paired else None
        if paired and (
            not isinstance(paired_audit, dict)
            or paired_audit.get("consistent") is not True
            or paired_audit.get("issues") != []
        ):
            raise ValueError(
                "Grouped review paired reports have unverified or conflicting content"
            )
        ca = {
            "checks": checks(
                first.get("conclusion"),
                [v["paragraph_id"] for v in conclusion.get("paragraphs", [])],
                "paragraph_id",
            )
        }
        flags = first.get("module")
        if (
            not isinstance(flags, list)
            or len(flags) != 4
            or any(type(v) is not bool for v in flags)
        ):
            raise ValueError("Grouped review module coverage incomplete")
        ca["module_check"] = dict(zip(MODULE_FIELDS, flags))
        states = first.get("states")
        expected = [v["item_id"] for v in context["established_reported_results"]]
        if not isinstance(states, list) or Counter(
            v[0] for v in states if isinstance(v, list) and v
        ) != Counter(expected):
            raise ValueError("Grouped review state coverage incomplete")
        ca["state_checks"] = []
        for row in states:
            if len(row) != 3 or any(type(v) is not bool for v in row[1:]):
                raise ValueError("Grouped review state verdict invalid")
            ca["state_checks"].append(
                {"item_id": row[0], "mentioned": row[1], "consistent": row[2]}
            )
        qa = {
            "checks": checks(
                first.get("questions"),
                [t["entry_id"] for t in targets],
                "entry_id",
                QUESTION_FIELDS,
            )
        }
        checked_c, errors_c = sy.validate(conclusion, ca, context)
        checked_q, errors_q = fu.validate(questions, qa, targets)
        notes = [v for _, _, errors in results for v in errors]
        cards = [v for _, chunk, _ in results for v in chunk]
        feedback = {"conclusion": errors_c, "questions": errors_q, "evidence": notes}
        if (
            errors_c
            or errors_q
            or checked_q["missing_entry_ids"]
            or notes
            or len(cards) != len(groups)
        ):
            continue
        sy.attach(report, checked_c, [])
        fu.attach(report, checked_q, [])
        final_groups, final_uses = ev.context_for(report, source, records)
        if c.digest(final_uses) != c.digest(uses) or c.digest(final_groups) != c.digest(
            groups
        ):
            raise ValueError("Grouped review candidate changed after audit")
        report["presentation"]["evidence_notes"] = {
            "version": ev.VERSION,
            "mode": "complete_literal_source",
            "items": cards,
            "uses": uses,
        }
        if paired:
            actual = c.render_markdown(report).split("## 附录：", 1)[0]
            if actual != paired_text:
                raise ValueError("Grouped review paired candidate changed after audit")
            cache = next(
                value["cache_file"]
                for value in reversed(calls)
                if value["stage"]
                == paired_stage
            )
            report["validation"]["grouped_paired"] = {
                "version": VERSION,
                "passed": True,
                "review_body_sha256": c.digest(actual),
                "achievement_draft_sha256": paired["draft_sha256"],
                "cache_file": cache,
                "audit": paired_audit,
            }
        report["validation"]["evidence_context_rejections"] = []
        report["validation"]["grouped_composition"] = {
            "version": VERSION,
            "content_sha256": content_digest(report),
        }
        return
    pipeline.write_json(
        pipeline.RUN / "failed-grouped" / f"{report['project_id']}-final.json",
        {"feedback": feedback, "draft": raw},
    )
    raise ValueError("Grouped review final composition did not pass checks")


def bind_assessment_plan_sources(draft, context):
    """Bind already-linked milestones before auditing the complete paragraph."""
    items = {item["item_id"]: item["source_ref"] for item in context["plan_items"]}
    sources = {source["source_id"]: source for source in context["sources"]}
    added = False
    for paragraph in draft.get("paragraphs", []):
        ids = paragraph.get("source_ids")
        linked = paragraph.get("item_ids")
        if (paragraph.get("has_assessment") is not True or not isinstance(ids, list)
                or not isinstance(linked, list) or not linked):
            continue
        selected = [sources[sid] for sid in ids if isinstance(sid, str) and sid in sources]
        if any(source["side"] == "plan" for source in selected):
            continue
        if not any(cite["role"] == "current" for cite in paragraph["citations"]):
            continue
        refs = {items[item_id] for item_id in linked if isinstance(item_id, str) and item_id in items}
        additions = []
        for source in sources.values():
            if source["side"] == "plan" and source["ref"] in refs:
                additions.append(source["source_id"])
                refs.remove(source["ref"])
        if additions:
            paragraph["source_ids"] = ids + additions
            added = True
    return sy.resolve(draft, context) if added else draft


def content_digest(report):
    return c.digest(
        {
            key: report["presentation"].get(key)
            for key in (
                "statements",
                "confirmation_notes",
                "acceptance_basis",
                "categorized_findings",
                "integrated_conclusion",
                "pi_confirmation",
                "evidence_notes",
            )
        }
    )


def finish(
    pipeline,
    pair,
    source,
    records,
    items,
    rows,
    inventory,
    errors,
    policy,
    config,
    chosen,
    bank,
    execute,
    rebuild_only,
    outdir,
    calls,
    started,
    reused,
    paired_provider=None,
):
    pipeline.require_comparison_coverage(items, rows)
    metadata = {
        "pipeline_version": pipeline.PIPELINE_VERSION,
        "prompt_version": prompts.PROMPT_VERSION,
        "model": config["text_model"],
        "review_workflow": "grouped",
        "policy_hash": c.digest(policy),
        "example_ids": [v["case_id"] for v in chosen],
        "example_approval_status": (
            bank.get("approval_status") if chosen and bank else "none"
        ),
        "calls": calls,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if reused:
        metadata["reused_review"] = reused
    report = initial(
        pipeline,
        pair,
        source,
        records,
        items,
        rows,
        inventory,
        errors,
        metadata,
        config,
        execute,
        rebuild_only,
        calls,
    )
    if "administrative_profile" in source:
        report["administrative_profile"] = copy.deepcopy(
            source["administrative_profile"]
        )
    if "source_document_links" in source:
        report["source_document_links"] = copy.deepcopy(source["source_document_links"])
    from project_report_citations import LAYOUT

    report["presentation"]["citation_layout"] = LAYOUT
    pipeline.add_report_view(
        report, source, records, config, execute, rebuild_only, calls, finalize=False
    )
    final(
        pipeline,
        report,
        source,
        records,
        config,
        execute,
        rebuild_only,
        calls,
        paired_provider,
    )
    report["run_metadata"]["wall_seconds"] = round(time.monotonic() - started, 2)
    return pipeline.save_report(report, source, outdir, calls)


ALIGNMENT_AUDIT = prompts.VERIFY_PROMPT + """
为避免重复抄写已经合格的对照内容，本轮使用补丁协议，替代上文完整alignments输出格式。
独立逐项检查全部plan_items和完整report_sources，不能因减少输出省略反向证据、阶段、指标单位、复合条件、成果状态与下一步计划。checked_ids必须每个item_id一次，表示完成独立核查，不表示自动通过。
未修改的行仅在完整支持时放入approved_ids；任何状态、来源、进度、比较口径、问题或延期判断需修改时，在replacements中给该项完整alignment（沿用上文字段），不能只改理由。无足够依据可改需要确认，不猜测。
approved_ids与replacements.item_id必须不重叠、不重复，且合起来恰好覆盖checked_ids及全部plan_items。不得将存在实质错误的行列入approved_ids。
只输出JSON {"checked_ids":["C001"],"approved_ids":["C001"],"replacements":[]}。
"""


def resolve_alignment_audit(answer, draft, items):
    expected = [item["item_id"] for item in items]
    checked = answer.get("checked_ids")
    approved = answer.get("approved_ids")
    patches = answer.get("replacements")
    original = draft.get("alignments")
    if (
        not isinstance(checked, list)
        or Counter(checked) != Counter(expected)
        or not isinstance(approved, list)
        or any(not isinstance(v, str) for v in approved)
        or not isinstance(patches, list)
        or any(not isinstance(v, dict) for v in patches)
        or not isinstance(original, list)
        or any(not isinstance(v, dict) for v in original)
    ):
        raise ValueError("Grouped review alignment audit coverage incomplete")
    if Counter(approved + [v.get("item_id") for v in patches]) != Counter(expected):
        raise ValueError("Grouped review alignment verdict coverage incomplete")
    by_id = {}
    for value in original:
        by_id.setdefault(value.get("item_id"), []).append(value)
    if any(len(by_id.get(key, [])) != 1 for key in approved):
        raise ValueError("Grouped review approved draft missing or duplicated")
    replacements = {v["item_id"]: v for v in patches}
    return {
        "alignments": [
            copy.deepcopy(replacements[key] if key in replacements else by_id[key][0])
            for key in expected
        ]
    }


def alignment_batches(items):
    """Group scopes without dropping conditions; return rows in original order later."""
    buckets = {}
    for item in items:
        buckets.setdefault(item["checkpoint_scope"], []).append(item)
    result = []
    for scope, values in buckets.items():
        limit = 12 if scope == "本轮节点" else 24
        batch = []
        size = 0
        for value in values:
            amount = len(str(value))
            if batch and (len(batch) >= limit or size + amount > 16000):
                result.append((len(result) + 1, batch))
                batch = []
                size = 0
            batch.append(value)
            size += amount
        if batch:
            result.append((len(result) + 1, batch))
    return result


def guard_current_progress(rows, records):
    """Current completion prose cannot borrow the plan's target number.

    This is a role-specific numeric gate, not a semantic judgement. Failed rows
    use the existing technical-abstention path and are never called a PI issue.
    """
    errors = []
    for row in rows:
        if row["status"] not in c.ASSERTIVE:
            continue
        support = " ".join(
            v["quote"] for v in row.get("evidence", []) if v.get("role") == "current"
        )
        if not p.number_support(row.get("progress", ""), support):
            row["validation_issues"] = list(
                dict.fromkeys(
                    row.get("validation_issues", [])
                    + ["unsupported_current_progress_number"]
                )
            )
            row.update(
                status="需要确认",
                semantic_reviewed=False,
                confirmation_kind="technical_validation",
            )
            errors.append(
                {
                    "item_id": row["item_id"],
                    "errors": ["unsupported_current_progress_number"],
                }
            )
    return rows, errors


def validate_paired_proof(review, achievement, root):
    from pathlib import Path

    root = Path(root).resolve()
    proof = review.get("validation", {}).get("grouped_paired", {})
    if proof.get("version") != VERSION or proof.get("passed") is not True:
        raise ValueError("Grouped review paired proof missing")
    path = (root / proof.get("cache_file", "")).resolve()
    if not path.is_relative_to(root / "runs") or not path.is_file():
        raise ValueError("Grouped review paired audit path invalid")
    import json

    saved = json.loads(path.read_text())
    pair = saved.get("request", {}).get("extra_modules", {}).get("paired_reports", {})
    text = c.render_markdown(review).split("## 附录：", 1)[0]
    audit = saved.get("answer", {}).get("paired_check")
    if (
        pair.get("paired_review") != text
        or pair.get("achievement_draft") != achievement["draft"]
        or proof.get("review_body_sha256") != c.digest(text)
        or proof.get("achievement_draft_sha256") != c.digest(achievement["draft"])
        or audit != proof.get("audit")
        or not isinstance(audit, dict)
        or audit.get("consistent") is not True
        or audit.get("issues") != []
    ):
        raise ValueError("Grouped review paired audit no longer binds final reports")
    if any(
        review["review_scope"][role + "_sha256"]
        != achievement["source"][role + "_sha256"]
        for role in ("plan", "report")
    ):
        raise ValueError("Grouped review paired materials differ")
    return True


def repair_numeric_sources(call, drafts, feedback, records):
    """Let the model bind missing numeric sources without rewriting prose.

    Retrieval proposes original passages only. The subsequent independent audit
    still verifies their semantic relationship, including object/stage/units.
    """
    requests = []
    values = {}
    owners = {}
    for rejected in feedback.get("reader", {}).get("rejected", []):
        value = rejected.get("value")
        if (
            not value
            or value.get("section") != "achievements"
            or "unsupported_display_number" not in rejected.get("errors", [])
        ):
            continue
        hints = p.numeric_source_hints(
            value["text"], value.get("citations", []), records
        )
        if not hints["candidate_refs"]:
            continue
        key = "reader:" + value["display_id"]
        requests.append(
            {
                "id": key,
                "fixed_text": value["text"],
                "missing_numbers": hints["missing_numbers"],
                "candidate_sources": [
                    {"ref": ref, **records[ref]}
                    for ref in hints["candidate_refs"]
                    if records[ref]["role"] in {"actual_or_body", "summary_and_next"}
                ],
            }
        )
        values[key] = next(
            v
            for v in drafts["reader"]["statements"]
            if v["display_id"] == value["display_id"]
        )
        owners[key] = "reader"
    if "basis" in feedback:
        rejected = {
            v.get("item", {}).get("item_id")
            for v in feedback["basis"].get("rejected", [])
            if "unsupported_acceptance_number" in v.get("errors", [])
        }
        for value in a.citation_candidates(drafts["basis"], records):
            if value["item_id"] not in rejected:
                continue
            key = "basis:" + value["item_id"]
            requests.append({"id": key, **value})
            values[key] = next(
                v for v in drafts["basis"]["items"] if v["item_id"] == value["item_id"]
            )
            owners[key] = "basis"
    if not requests:
        return set()
    answer = call(
        "grouped-source-binding",
        prompts.BOUNDARIES + """
只补选实际支持固定数字和事实的原文位置，不能修改文字、数字、状态、阶段、问题或原有引用。候选由程序按数字检索，仅为检索线索，不能因数字相同认定事实相同；须核对对象、指标、阶段、条件与完整上下文。引用未来安排不能证明已完成。
每个requests.id恰好输出一次，additional_refs只选该请求candidate_sources.ref。没有支持来源就返回空列表，不虚构编号。不删原有来源，不重写文章。
只输出JSON {"bindings":[{"id":"reader:S03","additional_refs":["R-P61"]}]}。
""",
        {"requests": requests},
        1600,
    )
    bindings = answer.get("bindings", [])
    if not isinstance(bindings, list) or Counter(
        v.get("id") for v in bindings if isinstance(v, dict)
    ) != Counter(list(values)):
        raise ValueError("Grouped review source binding coverage incomplete")
    proposed = {
        v["id"]: {source["ref"] for source in v["candidate_sources"]} for v in requests
    }
    updated = set()
    for binding in bindings:
        if set(binding) != {"id", "additional_refs"} or not isinstance(
            binding["additional_refs"], list
        ):
            raise ValueError("Grouped review source binding cannot change text")
        key = binding["id"]
        selected = binding["additional_refs"]
        if any(
            not isinstance(ref, str) or ref not in proposed[key] for ref in selected
        ) or len(selected) != len(set(selected)):
            raise ValueError("Grouped review source binding outside candidates")
        value = values[key]
        for ref in selected:
            if any(cite["ref"] == ref for cite in value["citations"]):
                continue
            cite, error = c.checked_citation(
                {"ref": ref, "quote": records[ref]["text"], "role": "current"}, records
            )
            if error:
                raise ValueError("Grouped review source binding not literal")
            value["citations"].append(cite)
            updated.add(owners[key])
    return updated


def checked_questions(items, rows):
    """Reuse only questions already authored and reviewed in alignment records."""
    by_id = {item["item_id"]: item for item in items}
    notes = []
    for row in rows:
        if (
            row["status"] != "需要确认"
            or row.get("confirmation_kind") != "business"
            or row.get("validation_issues")
            or not row.get("semantic_reviewed")
        ):
            continue
        if not row.get("question"):
            raise ValueError("Grouped review checked business question missing")
        item = by_id[row["item_id"]]
        notes.append(
            {
                "title": p.label(item),
                "text": row["question"],
                "item_ids": [row["item_id"]],
                "citations": [
                    {
                        "ref": item["source_ref"],
                        "quote": item["quote"],
                        "role": "target",
                    }
                ]
                + copy.deepcopy(row["evidence"]),
            }
        )
    return notes
