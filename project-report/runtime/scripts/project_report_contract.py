"""Source-bound report contract, validation and deterministic Markdown rendering."""
from __future__ import annotations

import calendar
import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import build_reliable_review as reliable
from review_common import recover_source_quote

SCHEMA_VERSION = "project-report-v0.11.0"
VALIDATION_VERSION = "project-report-validation-v0.3.3"
STATUSES = ("满足本期要求", "部分完成", "尚未完成", "需要确认", "后续节点", "总体目标参考")
DIMENSIONS = ("metric", "object", "unit", "stage", "test_conditions")
ASSERTIVE = set(STATUSES[:3])


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def numeric_tokens(text: str, *, chinese_counts: bool = False) -> set[str]:
    # Equivalent count notation only, without rounding or magnitude changes.
    text = re.sub(r'(?<!\d)\d{1,3}(?:[,，]\d{3})+(?!\d)',
                  lambda match: re.sub(r'[,，]', '', match[0]), text)
    digits = dict(zip('零〇一二两三四五六七八九', [0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9]))

    def count(match):
        value = match[0]
        if len(value) == 1 and value in digits:
            return str(digits[value])
        if re.fullmatch(r'[一二两三四五六七八九]?十[一二三四五六七八九]?', value):
            left, right = value.split('十')
            return str(digits.get(left, 1) * 10 + digits.get(right, 0))
        return value

    if chinese_counts:
        text = re.sub(r'[零〇一二两三四五六七八九十百千]+(?=个|类|种|条|项|层|轮|次|篇|件|台|套)', count, text)
    return {x.lower().lstrip('0') or '0' for x in re.findall(r"\d+(?:\.\d+)?", text)}


def numbers_supported(text: str, evidence: str) -> bool:
    return numeric_tokens(text) <= numeric_tokens(evidence, chinese_counts=True)


def asserts_delay(text: str) -> bool:
    for match in re.finditer(r'逾期|延期|超期|延误|晚于计划|超过计划(?:节点|期限)', text):
        prefix = text[max(0, match.start() - 18):match.start()]
        if not re.search(r'(?:不能|不可|不得|无法|暂不|尚不|不宜|不作|不)(?:直接|据此|认定|判定|判断|视为|构成|说明|确认|判|为|作|出){0,5}$', prefix):
            return True
    return False


def literal_quote(quote: Any, text: str) -> str | None:
    if not isinstance(quote, str) or len(quote.strip()) < 3:
        return None
    # Do not restore ellipses or guess a neighbouring source.
    return recover_source_quote(quote, text)


def relevant_residual(text: str) -> bool:
    text = re.sub(r'成果主要以[^：]*具体指标如下[：:]?', '', text)
    text = re.sub(r'性能指标|知识产权指标|团队建设与人才培养指标', '', text)
    stripped = re.sub(r"[\s\W_\d年月日至至到第阶段年度计划目标节点一二三四五六七八九十]", "", text)
    return len(stripped) >= 5


def build_inventory(source: dict, answer: dict) -> tuple[list[dict], dict]:
    """Keep literal model conditions; account for every uncovered source span."""
    blocks = source["plan_blocks"]
    periods = {x["id"]: x for x in source["plan_periods"]}
    candidates, rejected = [], []
    seen = set()
    for raw in answer.get("items", []):
        if not isinstance(raw, dict):
            rejected.append({"error": "invalid_inventory_item"})
            continue
        ref = raw.get("source_ref")
        quote = literal_quote(raw.get("quote"), blocks.get(ref, ""))
        if not quote or ref not in blocks:
            rejected.append({"item": raw, "error": "plan_quote_not_found"})
            continue
        key = (ref, quote)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"source_ref": ref, "quote": quote, "extraction_method": "model_literal"})
    coverage = []
    for ref, text in blocks.items():
        covered = [False] * len(text)
        for item in candidates:
            if item['source_ref'] != ref:
                continue
            start = text.find(item['quote'])
            if start < 0:
                # Whitespace normalization is accepted by literal_quote. Recovered
                # text is normally exact; do not fabricate coverage if not exact.
                continue
            covered[start:start + len(item['quote'])] = [True] * len(item['quote'])
        spans = list(re.finditer(r"[^。；\n]+[。；]?", text))
        residuals = []
        for span in spans:
            missing = ''.join(c for i, c in enumerate(text[span.start():span.end()], span.start()) if not covered[i])
            if relevant_residual(missing):
                residuals.append(span.group().strip())
                if (ref, span.group().strip()) not in seen:
                    candidates.append({"source_ref": ref, "quote": span.group().strip(),
                                       "extraction_method": "unparsed_source_fallback"})
                    seen.add((ref, span.group().strip()))
        content_chars = sum(c.isalnum() for c in text)
        covered_chars = sum(c.isalnum() and covered[i] for i, c in enumerate(text))
        coverage.append({"source_ref": ref, "source_characters": content_chars,
                         "model_covered_characters": covered_chars,
                         "model_coverage_ratio": round(covered_chars / content_chars, 4) if content_chars else 1,
                         "residual_spans": residuals})
    order = {ref: i for i, ref in enumerate(blocks)}
    candidates.sort(key=lambda x: (order[x['source_ref']], max(0, blocks[x['source_ref']].find(x['quote']))))
    for index, item in enumerate(candidates, 1):
        period = periods.get(item['source_ref'])
        item.update({"item_id": f"C{index:03d}", "requirement": item['quote'],
                     "scope": "milestone" if period else "overall_reference",
                     "due": period['due'] if period else {"date": None, "precision": None},
                     "checkpoint_scope": period['checkpoint_scope'] if period else '总体目标参考',
                     "period_heading": period['heading'] if period else '总体成果与考核指标'})
    if not candidates:
        raise ValueError('No verifiable plan conditions; report generation stopped')
    return candidates, {"blocks": coverage, "rejected": rejected,
                        "fallback_conditions": sum(x['extraction_method'] != 'model_literal' for x in candidates),
                        "note": "字符覆盖用于发现遗漏，不证明模型已完成语义原子化；未拆解相关段落显式保留。"}


def report_sources(source: dict) -> dict[str, dict]:
    records = {}
    labels = r"(?:计划要求（报告转述）：|目标完成情况：|项目组自评：|报告列出的证明材料：)"
    roles = {'计划要求（报告转述）：': ('TARGET', 'target_restatement'),
             '目标完成情况：': ('ACTUAL', 'actual_or_body'),
             '项目组自评：': ('SELF', 'self_assessment'),
             '报告列出的证明材料：': ('PROOF', 'proof_label')}
    for ref, text in source['report_blocks'].items():
        if ref.startswith('R-T1-'):
            markers = list(re.finditer(labels, text))
            for i, marker in enumerate(markers):
                end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
                value = text[marker.end():end].strip()
                if value:
                    suffix, role = roles[marker.group()]
                    records[f'{ref}-{suffix}'] = {'text': value, 'role': role, 'base_ref': ref}
        else:
            records[ref] = {'text': text, 'role': 'summary_and_next' if ref.startswith('R-S-') else 'actual_or_body', 'base_ref': ref}
    if not any(x['role'] in {'actual_or_body', 'summary_and_next'} for x in records.values()):
        raise ValueError('No progress source text')
    return records


def checked_citation(raw: Any, records: dict, future: bool = False) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, 'invalid_citation_shape'
    ref = raw.get('ref')
    record = records.get(ref)
    if not record:
        return None, 'invalid_report_ref'
    quote = literal_quote(raw.get('quote'), record['text'])
    if not quote:
        return None, 'report_quote_not_found'
    role = 'future' if future else raw.get('role')
    if role not in {'current', 'future', 'target'}:
        return None, 'invalid_citation_role'
    if role == 'current' and record['role'] in {'target_restatement', 'proof_label', 'self_assessment'}:
        return None, 'target_or_label_used_as_current_fact'
    if future and record['role'] in {'target_restatement', 'proof_label', 'self_assessment'}:
        return None, 'label_used_as_next_step'
    if role == 'current' and re.match(r'^(?:[（(]?\d+[）)、.]\s*)?(?:下一步|下一阶段|未来|后续将|拟|计划将|将继续|将开展)', quote):
        return None, 'future_used_as_current_fact'
    return {'ref': ref, 'quote': quote, 'role': role}, None


def generic_pending(item: dict, reason: str = '未能建立完整、可比的计划与进展对应关系。') -> dict:
    return {'item_id': item['item_id'], 'status': '需要确认', 'progress': '本项暂未形成可直接采用的完整进展判断。',
            'reason': reason, 'comparability': {k: 'unknown' for k in DIMENSIONS},
            'evidence': [], 'next_steps': [], 'binding': None,
            'question': '请说明本项要求对应的当前进展、统计口径及适用阶段。', 'overdue': False}


def validate_alignments(items: list[dict], answer: dict, records: dict, source: dict) -> tuple[list[dict], list[dict]]:
    raw_list = answer.get('alignments', [])
    by_id = {}
    errors = []
    valid_ids = {x['item_id'] for x in items}
    for raw in raw_list:
        if not isinstance(raw, dict) or raw.get('item_id') not in valid_ids:
            errors.append({'item_id': raw.get('item_id') if isinstance(raw, dict) else None, 'errors': ['unknown_item_id']})
            continue
        by_id.setdefault(raw['item_id'], []).append(raw)
    accepted = []
    for item in items:
        item_errors = []
        rows = by_id.get(item['item_id'], [])
        if len(rows) != 1:
            item_errors.append('missing_or_duplicate_alignment')
            value = generic_pending(item, '模型未返回唯一的逐项判断，需补充对应关系。')
        else:
            value = copy.deepcopy(rows[0])
        if value.get('status') not in STATUSES:
            item_errors.append('invalid_status')
        citations = []
        for raw in value.get('evidence', []):
            citation, error = checked_citation(raw, records)
            if error:
                item_errors.append(error)
            else:
                citations.append(citation)
        next_steps = []
        for raw in value.get('next_steps', []):
            citation, error = checked_citation(raw, records, future=True)
            if error:
                item_errors.append(error)
            else:
                next_steps.append(citation)
        value['evidence'], value['next_steps'] = citations, next_steps
        semantic_overrides = []
        if value.get('status') in {'部分完成', '尚未完成', '需要确认'}:
            progress_blocks = {ref: record['text'] for ref, record in records.items()
                               if record['role'] in {'actual_or_body', 'summary_and_next'}}
            for citation in list(citations):
                if citation['role'] != 'current':
                    continue
                conflict = reliable.base.report_status_conflict({'report_blocks': progress_blocks},
                            {'plan_quote': item['quote'], 'report_ref': citation['ref'], 'report_quote': citation['quote']})
                if not conflict:
                    continue
                ref, quote, spec = conflict
                if not any(x['ref'] == ref and x['quote'] == quote for x in citations):
                    citations.append({'ref': ref, 'quote': quote, 'role': 'current'})
                semantic_overrides.append({'rule': 'completion_and_progress_conflict', 'spec': spec,
                                           'original_status': value['status'], 'counterevidence_ref': ref,
                                           'progress_ref': citation['ref']})
                value.update(status='需要确认', progress='报告对本项同时存在完成及推进描述，需确认对应阶段与范围。',
                             reason='已找到同一规格的完成陈述与推进陈述，本项暂不认定为确定性未完成。',
                             question='请明确完成陈述与推进陈述分别对应的任务阶段、验证范围及当前状态。', overdue=False)
                break
        binding = value.get('binding')
        if binding:
            cited, error = checked_citation({**binding, 'role': 'target'}, records)
            record = records.get(binding.get('ref'), {})
            pdf_binding = ('-PDF-P' in str(binding.get('ref'))
                           and bool(re.search(r'中期目标|中期任务', record.get('text', '')))
                           and '目标' in str(binding.get('quote', ''))
                           and bool(numeric_tokens(item['quote']) & numeric_tokens(str(binding.get('quote', '')))))
            if error or (record.get('role') != 'target_restatement' and not pdf_binding):
                item_errors.append('invalid_midterm_binding')
                value['binding'] = None
            else:
                value['binding'] = cited
        if value.get('status') in {'部分完成', '尚未完成'} and re.search(r'未明确|未提及|未说明|未直接说明|未直接提及', str(value.get('reason', ''))):
            current_text = ' '.join(x['quote'] for x in citations if x['role'] == 'current')
            explicit_incomplete = bool(re.search(r'尚未|未完成|部分完成|仍在|正在|当前.*推进|目前完成', current_text))
            explicit_incomplete |= bool('目标' in current_text and any(float(x) < 100 for x in re.findall(r'(\d+(?:\.\d+)?)\s*[%％]', current_text)))
            if not explicit_incomplete:
                semantic_overrides.append({'rule': 'absence_does_not_prove_incomplete', 'original_status': value['status']})
                value.update(status='需要确认', reason='现有引文未建立全部计划条件的对应关系，不能据未提及认定工作未完成。',
                             question='请说明本项各子条件对应的实际进展；未明确的内容目前达到什么程度？', overdue=False)
        # An explicit self-reported target/progress percentage can deserve a
        # phase question, without turning an unbound final goal into a breach.
        attention = ((value.get('attention') is True or
                      (value.get('status') == '需要确认' and (value.get('comparability') or {}).get('stage') == 'unknown'
                       and re.search(r'阶段|本期|中期|终期|考核', str(value.get('question', '')))))
                     and all((value.get('comparability') or {}).get(k) == 'yes' for k in ('metric', 'object', 'unit'))
                     and any(x['role'] == 'current' and '目标' in x['quote'] and '完成' in x['quote']
                             and any(float(n) < 100 for n in re.findall(r'(\d+(?:\.\d+)?)\s*[%％]', x['quote'])) for x in citations))
        citation_warnings = []
        if attention and item['scope'] == 'overall_reference' and not value.get('binding'):
            value['status'] = '需要确认'
            value['overdue'] = False
            value['question'] = value.get('question') or '请确认该总体规模目标是否属于本期要求，以及后续剩余任务的安排。'
            progress_quote = next(x['quote'] for x in citations if x['role'] == 'current' and '目标' in x['quote']
                                  and '完成' in x['quote'] and re.search(r'\d+(?:\.\d+)?\s*[%％]', x['quote']))
            value['progress'] = '报告原文：' + progress_quote
            value['reason'] = '报告自行给出了目标及完成比例，但该总体目标是否纳入本期要求仍需确认。'
            # This is a phase question, not a conclusive gap. Discard invalid
            # supplementary quotations, retain the verified progress quotation,
            # and log the omission; do not weaken conclusive-judgement gates.
            citation_warnings = [e for e in item_errors if e in {'report_quote_not_found', 'invalid_report_ref'}]
            item_errors = [e for e in item_errors if e not in {'report_quote_not_found', 'invalid_report_ref'}]
        elif item['scope'] == 'overall_reference' and not value.get('binding'):
            value['status'] = '总体目标参考'
            value['overdue'] = False
        if item['checkpoint_scope'] == '后续节点':
            value['status'] = '后续节点'
            value['overdue'] = False
        status = value.get('status')
        if status == '后续节点' and item['checkpoint_scope'] != '后续节点':
            item_errors.append('current_condition_misclassified_as_future')
        if status in ASSERTIVE:
            if not any(x['role'] == 'current' for x in citations):
                item_errors.append('assertion_without_current_fact')
            comparability = value.get('comparability') or {}
            if any(comparability.get(k) != 'yes' for k in DIMENSIONS):
                item_errors.append('comparability_not_established')
        claim = ' '.join(str(value.get(k) or '') for k in ('progress', 'reason', 'question'))
        # A known source label (R-P41) is metadata, not a measured value 41.
        # Ignore only exact registered refs; an invented source ID or an actual
        # quantity outside the matched label must still fail numeric support.
        numeric_claim = claim
        for ref in sorted(records, key=len, reverse=True):
            numeric_claim = re.sub(r'(?<![A-Za-z0-9_-])' + re.escape(ref) + r'(?![A-Za-z0-9_-])', '', numeric_claim)
        supporting = item['quote'] + ' '.join(x['quote'] for x in citations + next_steps)
        supporting += source['checkpoint_date'] + str(item['due'].get('date') or '')
        if not numbers_supported(numeric_claim, supporting):
            item_errors.append('unsupported_generated_number')
        if not isinstance(value.get('progress'), str) or not isinstance(value.get('reason'), str) or not value.get('reason'):
            item_errors.append('missing_progress_or_reason')
        positive_delay = bool(value.get('overdue')) or asserts_delay(claim)
        if positive_delay:
            bounds = reliable.evidence_date_bounds(source['evidence_as_of'])
            due = item['due'].get('date')
            current_quotes = ' '.join(x['quote'] for x in citations if x['role'] == 'current')
            if not due or not bounds or bounds[0] <= dt.date.fromisoformat(due) \
                    or status not in {'部分完成', '尚未完成'} \
                    or not re.search(r'尚未|未完成|部分完成|仍在|正在|目前完成', current_quotes):
                item_errors.append('unsupported_overdue')
        else:
            value['overdue'] = False
        if item['extraction_method'] == 'unparsed_source_fallback' and status in ASSERTIVE:
            item_errors.append('unparsed_condition_cannot_be_conclusive')
        if item_errors:
            errors.append({'item_id': item['item_id'], 'errors': sorted(set(item_errors)), 'rejected': copy.deepcopy(value)})
            # Do not retain an unvalidated model claim or explanatory sentence.
            replacement = generic_pending(item, '本项模型判断未通过完整性、引文或边界校验，暂列待确认；不据此认定工作未开展。')
            replacement['evidence'], replacement['next_steps'] = citations, next_steps
            if item['scope'] == 'overall_reference' and not value.get('binding'):
                replacement['status'] = '总体目标参考'
            elif item['checkpoint_scope'] == '后续节点':
                replacement['status'] = '后续节点'
            value = replacement
        value['validation_issues'] = sorted(set(item_errors))
        if attention and not item_errors and value.get('status') == '需要确认':
            value['attention'] = True
        else:
            value.pop('attention', None)
        if semantic_overrides:
            value['semantic_overrides'] = semantic_overrides
        if citation_warnings:
            value['citation_warnings'] = citation_warnings
        value['confirmation_kind'] = ('technical_validation' if item_errors else 'business') if value.get('status') == '需要确认' else None
        value['semantic_reviewed'] = len(rows) == 1 and not item_errors
        accepted.append(value)
    return accepted, errors


def apply_citation_repairs(original: dict, repaired: dict, allowed_ids: set[str]) -> dict:
    """A repair can replace citations only; it cannot change a judgement."""
    updated = copy.deepcopy(original)
    changes = {}
    for patch in repaired.get('repairs', []):
        if isinstance(patch, dict) and patch.get('item_id') in allowed_ids:
            changes.setdefault(patch['item_id'], []).append(patch)
    for row in updated.get('alignments', []):
        patches = changes.get(row.get('item_id'), [])
        if len(patches) != 1:
            continue
        for key in ('evidence', 'next_steps', 'binding'):
            if key in patches[0]:
                row[key] = patches[0][key]
    return updated


def resolve_citation_repairs(repaired: dict, records: dict) -> dict:
    """Resolve source selections before an independent semantic repair audit.

    Extracted PDF cells may interleave. Resolve a selected block rather than
    asking a writer to reconstruct sentences. Explicit quotes remain strict.
    """
    result = copy.deepcopy(repaired)
    for patch in result.get('repairs', []):
        if not isinstance(patch, dict):
            continue
        citations = list(patch.get('evidence', [])) + list(patch.get('next_steps', []))
        if isinstance(patch.get('binding'), dict):
            citations.append(patch['binding'])
        for citation in citations:
            if isinstance(citation, dict) and 'quote' not in citation and citation.get('ref') in records:
                citation['quote'] = records[citation['ref']]['text']
    return result


def narrative_support(statement: dict, items: dict, alignments: dict) -> str:
    parts = []
    for key in statement.get('item_ids', []):
        if key not in items or key not in alignments:
            continue
        item, row = items[key], alignments[key]
        parts += [item['quote'], row['progress'], row['reason']]
        parts += [x['quote'] for x in row['evidence']]
        # Future plans are excluded from achievement support.
        if statement.get('section') != 'achievements':
            parts += [x['quote'] for x in row['next_steps']]
    return ' '.join(parts)


def validate_statements(raw: dict, audit: dict, items: list[dict], alignments: list[dict]) -> tuple[list[dict], list[dict]]:
    item_map = {x['item_id']: x for x in items}
    alignment_map = {x['item_id']: x for x in alignments}
    checks = {}
    for check in audit.get('checks', []):
        if isinstance(check, dict):
            checks.setdefault(check.get('statement_id'), []).append(check)
    accepted, rejected = [], []
    for statement in raw.get('statements', []):
        errors = []
        if not isinstance(statement, dict):
            rejected.append({'errors': ['invalid_statement_shape']})
            continue
        linked = statement.get('item_ids', [])
        text = statement.get('text', '')
        if statement.get('section') not in {'overview', 'achievements', 'conclusion'} or not isinstance(text, str) or not text:
            errors.append('invalid_statement_shape')
        if not linked or any(x not in item_map for x in linked):
            errors.append('invalid_statement_item_refs')
        support = narrative_support(statement, item_map, alignment_map)
        if not numbers_supported(str(text), support):
            errors.append('unsupported_statement_number')
        if re.search(r'通过验收|通过评审|评分|得分|整体风险(?:较高|较低|可控)|全部(?:任务|目标|要求)?(?:均已)?完成|所有.*(?:均已完成|均已满足)', str(text)):
            errors.append('unsupported_global_rating')
        if statement.get('section') == 'achievements':
            if not any(x['role'] == 'current' for key in linked if key in alignment_map for x in alignment_map[key]['evidence']):
                errors.append('achievement_without_current_source')
        rows = checks.get(statement.get('statement_id'), [])
        if len(rows) != 1 or rows[0].get('supported') is not True:
            errors.append('semantic_narrative_audit_failed')
        if errors:
            rejected.append({'statement': statement, 'errors': errors, 'audit': rows})
        else:
            accepted.append(statement)
    return accepted, rejected


def location(ref: str) -> str:
    if ref == 'P-CORE':
        return '总体成果形式、考核指标与评测方式'
    match = re.search(r'PDF-P(\d+)', ref)
    if match:
        return f'第 {match.group(1)} 页（PDF 文件页序）'
    if ref.startswith('P-T'):
        numbers = re.findall(r'\d+', ref)
        if '-N' in ref and len(numbers) >= 4:
            return f'里程碑表：第 {numbers[0]} 表、第 {numbers[1]} 行、内嵌第 {numbers[2]} 表第 {numbers[3]} 行'
        return f'里程碑表：第 {numbers[0]} 表、第 {numbers[1]} 行、第 {numbers[2]} 段组' if len(numbers) >= 3 else '里程碑章节'
    match = re.search(r'R-T(\d+)-R(\d+)', ref)
    if match:
        suffix = ref.rsplit('-', 1)[-1]
        label = {'ACTUAL': '目标完成情况', 'TARGET': '中期目标要求', 'SELF': '项目组自评', 'PROOF': '证明材料名称'}.get(suffix, '目标表')
        return f'第 {match.group(1)} 表、第 {match.group(2)} 行，{label}栏'
    match = re.search(r'-P(\d+)', ref)
    if match:
        return ('总结及下一步计划，' if ref.startswith('R-S-') else '实施正文，') + f'第 {match.group(1)} 个正文段落（按文件段落序）'
    return ref


def source_index(source: dict, records: dict, items: list[dict], alignments: list[dict]) -> tuple[list[dict], dict]:
    index, lookup = [], {}
    def add(ref, quote, side):
        key = (ref, quote)
        if key not in lookup:
            sid = f'依据{len(index) + 1:02d}'
            lookup[key] = sid
            index.append({'source_id': sid, 'side': side, 'file_name': Path(source['plan_file' if side == 'plan' else 'report_file']).name,
                          'ref': ref, 'location': location(ref), 'quote': quote})
    for item in items:
        add(item['source_ref'], item['quote'], 'plan')
    for row in alignments:
        for citation in row['evidence'] + row['next_steps'] + ([row['binding']] if row.get('binding') else []):
            add(citation['ref'], citation['quote'], 'report')
    return index, lookup


def cell(text: Any) -> str:
    return re.sub(r'\s+', ' ', str('—' if text is None or text == '' else text)).replace('|', '\\|').replace('<', '&lt;').replace('>', '&gt;')


def render_markdown(report: dict) -> str:
    from project_report_presentation import render
    return render(report)


def assemble_report(source: dict, records: dict, items: list, alignments: list, inventory_validation: dict,
                    alignment_errors: list, statements: list, narrative_rejections: list,
                    metadata: dict) -> dict:
    index, _ = source_index(source, records, items, alignments)
    return {'schema_version': SCHEMA_VERSION, 'project_id': source['project_id'],
            'project_profile': {'project_name': Path(source['plan_file']).stem},
            'review_scope': {**{k: source[k] for k in ('review_stage', 'checkpoint_date', 'evidence_as_of', 'cover_date',
                                                   'plan_file', 'report_file', 'plan_sha256', 'report_sha256')},
                             'ocr': source.get('ocr', {'scanned': False})},
            'plan_items': items, 'alignments': alignments,
            'progress_claims': [{'item_id': row['item_id'], **cite} for row in alignments for cite in row['evidence'] if cite['role'] == 'current'],
            'next_step_plans': [{'item_id': row['item_id'], **cite} for row in alignments for cite in row['next_steps']],
            'findings': [row for row in alignments if row['status'] in {'部分完成', '尚未完成'}],
            'confirmation_items': [row for row in alignments if row['status'] == '需要确认'],
            'attention_items': [row for row in alignments if row.get('attention')],
            'overall_assessment': {'statements': statements}, 'source_index': index,
            'manager_feedback': {'collection_status': 'not_started', 'overall': None, 'items': []},
            'validation': {'version': VALIDATION_VERSION, 'condition_records_complete': len(items) == len(alignments),
                           'semantically_reviewed_conditions': sum(x['semantic_reviewed'] for x in alignments),
                           'fallback_conditions': inventory_validation['fallback_conditions'],
                           'inventory': inventory_validation, 'alignment_errors': alignment_errors,
                           'narrative_rejections': narrative_rejections,
                           'status': 'draft_with_confirmations' if alignment_errors or inventory_validation['fallback_conditions'] else 'draft_ready'},
            'run_metadata': metadata}
