"""Achievement expression v0.3: checked facts, readable narrative, centralized sources.

Legacy reports remain immutable/readable. Protocol completion cannot overrule a
failed content audit. Source repair and bounded text repair precede final audit.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import achievement_expression_prompts as prompts
import achievement_audit_protocol as audit_protocol
import achievement_sources as sources
import build_achievement_reports as legacy
import build_contract_review as base
import project_report_contract as contract
import project_report_presentation as presentation
import project_report_profile as profile
from llm_runtime import call_scope, model_config

VERSION = 'achievement-expression-v0.5.0'
ROOT = base.ROOT
QUESTIONS = {'problem': '重要问题', 'delivered': '已经做成什么', 'increment': '核心增量',
             'results': '结果与对标', 'value': '科学或应用价值', 'stage': '阶段与归属'}
KINDS = {'事实归纳', '机制解释', '示意说明', '价值分析', '后续方向'}
FACT_FIELDS = ('supported', 'state_preserved', 'scope_preserved', 'conflicts_preserved')


def is_expression(report):
    return report.get('schema_version') in {VERSION, 'achievement-expression-v0.4.0', 'achievement-expression-v0.3.0'}


def statements(draft):
    items = [{'statement_id': 'T01', 'text': draft.get('value_title', ''),
              'fact_ids': draft.get('title_fact_ids', []), 'kind': '价值分析'}]
    items += list(draft.get('fact_sheet', {}).values())
    items += draft.get('short_version', [])
    items += draft.get('long_intro', [])
    for index, section in enumerate(draft.get('long_sections', []), 1):
        keys = section.get('title_fact_ids', list(dict.fromkeys(k for p in section['paragraphs'] for k in p.get('fact_ids', []))))
        items.append({'statement_id': f'H{index:02d}', 'text': section['title'],
                      'kind': '价值分析' if keys else '机制解释', 'fact_ids': keys})
        items += section.get('paragraphs', [])
    items += draft.get('questions', [])
    items += list(draft.get('card', {}).values())
    return items


def normalize(raw, facts):
    """Assign IDs ourselves and reject unsupported material numbers/unknown bindings."""
    if not isinstance(raw, dict):
        raise ValueError('Expression must be a JSON object')
    draft = copy.deepcopy(raw); errors = []
    facts = {f['fact_id']: f for f in facts}
    if not isinstance(draft.get('value_title'), str) or not draft['value_title'].strip():
        raise ValueError('Expression title missing')
    if set(draft.get('fact_sheet', {})) != set(QUESTIONS):
        raise ValueError('Expression needs all six fact questions')
    draft['fact_sheet'] = {key: draft['fact_sheet'][key] for key in QUESTIONS}
    # A complete short version may be a single natural paragraph. Paragraph
    # count is editorial style; absence/type errors still stop publication.
    if (not isinstance(draft.get('short_version'), list) or not 1 <= len(draft['short_version']) <= 5
            or not isinstance(draft.get('long_sections'), list) or not 4 <= len(draft['long_sections']) <= 8):
        raise ValueError('Expression short/long structure incomplete')
    if not isinstance(draft.get('questions', []), list) or len(draft.get('questions', [])) > 3:
        raise ValueError('Too many confirmation questions')
    if draft.get('narrative_format') in {'reader-questions-v1', 'reader-questions-v2'} and not 1 <= len(draft.get('long_intro', [])) <= 2:
        raise ValueError('Reader narrative needs a short opening scene')
    card_keys = ('delivered', 'method', 'value', 'stage', 'next')
    if draft.get('narrative_format') == 'reader-questions-v2' and set(draft.get('card', {})) != set(card_keys):
        raise ValueError('Achievement card needs results, method, value, stage and next step')
    if draft.get('card'):
        draft['card'] = {key: draft['card'][key] for key in card_keys}
        for index, value in enumerate(draft['card'].values(), 1):
            value['statement_id'] = f'G{index:02d}'
    for i, value in enumerate(draft['fact_sheet'].values(), 1): value['statement_id'] = f'Q{i:02d}'
    for i, value in enumerate(draft['short_version'], 1): value['statement_id'] = f'B{i:02d}'
    for i, value in enumerate(draft.get('long_intro', []), 1): value['statement_id'] = f'N{i:02d}'
    index = 0
    for section in draft['long_sections']:
        if not isinstance(section.get('title'), str) or not section['title'].strip() or not section.get('paragraphs'):
            raise ValueError('Long section title/paragraphs missing')
        # Heading claims are covered by the material audit of the whole draft.
        for value in section['paragraphs']:
            index += 1; value['statement_id'] = f'L{index:02d}'
    for i, value in enumerate(draft.get('questions', []), 1): value['statement_id'] = f'K{i:02d}'
    for value in statements(draft):
        sid = value['statement_id']; text = value.get('text', '')
        keys = value.get('fact_ids')
        if not isinstance(text, str) or not text.strip() or value.get('kind') not in KINDS:
            errors.append({'statement_id': sid, 'reason': 'invalid_statement_fields'}); continue
        if not isinstance(keys, list) or any(not isinstance(k, str) or k not in facts for k in keys):
            errors.append({'statement_id': sid, 'reason': 'unknown_fact_binding'}); continue
        if value['kind'] not in {'机制解释', '示意说明'} and not keys:
            errors.append({'statement_id': sid, 'reason': 'missing_fact_binding'})
        if sid.startswith('G') and not keys:
            errors.append({'statement_id': sid, 'reason': 'missing_card_fact_binding'})
        support = '\n'.join(facts[k]['text'] + '\n' + '\n'.join(c['quote'] for c in facts[k]['citations']) for k in keys)
        # General explanation is allowed; invented numerical examples are deliberately
        # outside this first format, rather than silently treated as project results.
        if not presentation.number_support(text, support):
            errors.append({'statement_id': sid, 'reason': 'project_number_without_source'})
        if re.search(r'\bF\d{2,}\b|\]\(|<a\b|^#{1,6}\s', text):
            errors.append({'statement_id': sid, 'reason': 'internal_markup_in_prose'})
    return draft, errors


def semantic_records(source):
    return {ref: {k: v for k, v in record.items() if k != 'file'} for ref, record in source['records'].items()}


def read_reusable_facts(catalog_path, source):
    """Bind old approvals to identical source bytes/roles/text, not project names."""
    catalog_path = Path(catalog_path).resolve()
    if not catalog_path.is_relative_to(ROOT / 'runs'):
        raise ValueError('Fact catalog must be a private workspace run')
    catalog = json.loads(catalog_path.read_text())
    old_path = catalog_path.parent.parent / 'sources' / catalog_path.name
    old = json.loads(old_path.read_text())
    if old.get('version') != source['version'] or old['project_id'] != source['project_id']:
        raise ValueError('Fact source version or project mismatch')
    if any(old[role + '_sha256'] != source[role + '_sha256'] for role in ('plan', 'report')) or semantic_records(old) != semantic_records(source):
        raise ValueError('Fact source bytes, roles or sections changed')
    facts = catalog['facts']; audit = catalog['audit']
    _, errors = legacy.resolve_facts({'facts': facts}, source['records'])
    errors += legacy.audit_checks(audit, 'fact_id', [f['fact_id'] for f in facts], FACT_FIELDS)
    if errors:
        raise ValueError('Fact catalog is not fully checked: ' + str(errors))
    # Follow the actual audit call, rather than trusting a copied all-true array.
    calls = [c for c in catalog.get('calls', []) if c.get('stage') == 'facts-audit']
    if not calls:
        raise ValueError('Fact catalog has no independent audit provenance')
    audit_path = (ROOT / calls[-1]['cache_file']).resolve()
    if not audit_path.is_relative_to(ROOT / 'runs'):
        raise ValueError('Fact audit is outside private runs')
    original = json.loads(audit_path.read_text())
    request = original['request']
    original_records = {r['ref']: {k: v for k, v in r.items() if k != 'ref'} for r in request.get('source_records', [])}
    if (original.get('answer') != audit or request.get('facts_draft') != facts or
        request.get('framework_method') != legacy.framework()['method_only'] or original_records != old['records']):
        raise ValueError('Fact audit request/response does not match the catalog')
    proof = {'catalog_file': str(catalog_path.relative_to(ROOT)), 'catalog_sha256': base.sha256(catalog_path),
             'source_file': str(old_path.relative_to(ROOT)), 'source_sha256': base.sha256(old_path),
             'audit_file': str(audit_path.relative_to(ROOT)), 'audit_sha256': base.sha256(audit_path)}
    return copy.deepcopy(facts), copy.deepcopy(audit), proof


def prepare_facts(source, run, config, execute, facts_from=None):
    pid = source['project_id']; paths = [Path(facts_from)] if facts_from else []
    if not facts_from:
        paths = sorted((ROOT / 'runs').glob(f'*/facts/{pid}.json'))
        paths += sorted((ROOT / 'runs').glob(f'*/pipelines/*/achievement/facts/{pid}.json'))
    for path in paths:
        try:
            facts, audit, proof = read_reusable_facts(path, source)
            return facts, audit, [], proof
        except (ValueError, KeyError, TypeError, OSError):
            if facts_from: raise
    context = {'project_id': pid, 'project_name': source['project_name'],
        'framework_method': legacy.framework()['method_only'], 'input_policy': source['input_policy'],
        'source_records': [{'ref': ref, **record} for ref, record in source['records'].items()]}
    calls = []; feedback = []
    for attempt in range(3):
        citation_only = bool(feedback) and all(isinstance(error, str) and error.startswith(
            ('fact_number_unsupported:', 'fact_quote_missing:', 'fact_no_source:')) for error in feedback['errors'])
        if citation_only:
            # Keep already written facts fixed. Source selection is narrower
            # than rewriting the whole catalog and still needs a fresh audit.
            raw = copy.deepcopy(feedback['previous_draft'])
        else:
            raw, usage = legacy.cached_call(run, pid, f'facts-edit-{attempt}', legacy.prompts.FACTS,
                {**context, 'editing_feedback': feedback}, config, execute)
            calls.append({'stage': 'facts-edit', **usage})
        facts, errors = legacy.resolve_facts(raw, source['records'])
        if errors:
            raw = legacy.repair_fact_citations(raw, errors, source, run, pid, config, execute, attempt, calls,
                feedback={'errors': feedback['errors'], 'instruction': '上次选择仍未通过。仅补选支持固定事实的原文位置，不能重复无效选择。'} if citation_only else None)
            facts, errors = legacy.resolve_facts(raw, source['records'])
        audit = {}
        if not errors:
            audit, usage = legacy.cached_call(run, pid, f'facts-audit-{attempt}', legacy.prompts.FACT_AUDIT,
                {**context, 'facts_draft': facts}, config, execute)
            calls.append({'stage': 'facts-audit', **usage})
            errors += legacy.audit_checks(audit, 'fact_id', [f['fact_id'] for f in facts], FACT_FIELDS)
        if not errors: return facts, audit, calls, None
        required = {'问题背景', '代表成果', '核心增量', '结果与应用', '后续计划'}
        feedback = {'previous_draft': raw, 'errors': errors, 'audit': audit,
                    'missing_categories': sorted(required - {fact.get('category') for fact in facts}),
                    'instruction': '修复所列错误。缺少代表成果等分类时，从已有且有依据的事实中选择适合者调整类别，不虚构新成果。'}
    legacy.write_json(Path(run) / 'failed-facts' / (pid + '.json'),
                      {'stage': 'fact_preparation', 'errors': errors, 'draft': raw, 'audit': audit})
    raise ValueError('Achievement fact preparation failed; raw calls retained')


def audit_errors(draft, audit, paired=False):
    errors = legacy.audit_checks(audit, 'statement_id', [s['statement_id'] for s in statements(draft)], ('material_fact_ok',))
    issues = audit.get('material_issues')
    if not isinstance(issues, list) or any(not isinstance(i, dict) or i.get('statement_id') not in {s['statement_id'] for s in statements(draft)} for i in issues):
        errors.append('invalid_material_issue_list')
    elif issues: errors += issues
    if paired and audit.get('paired_consistent') is not True: errors.append('paired_material_conflict')
    return errors


def apply_changes(draft, changes, targets, facts, *, allow_remaining=False):
    values = changes.get('changes', [])
    if len(values) != len(targets) or {v.get('statement_id') for v in values} != set(targets):
        raise ValueError('Targeted repair must cover exactly the requested statements')
    result = copy.deepcopy(draft)
    mapping = {v['statement_id']: v for v in statements(result)}
    for value in values:
        sid = value['statement_id']
        if sid == 'T01':
            result['value_title'], result['title_fact_ids'] = value['text'], value['fact_ids']
        elif sid.startswith('H'):
            result['long_sections'][int(sid[1:]) - 1]['title'] = value['text']
            result['long_sections'][int(sid[1:]) - 1]['title_fact_ids'] = value['fact_ids']
        else:
            mapping[sid].update({key: value[key] for key in ('text', 'kind', 'fact_ids')})
    result, errors = normalize(result, facts)
    if errors and not allow_remaining:
        raise ValueError('Targeted repair still violates source constraints: ' + str(errors))
    return result


def numeric_repair_feedback(draft, targets, facts):
    """Give the editor exact rejected numerals and the bound source, not just a label.

    This is diagnostic context only. It never changes a number, expands a fact
    binding, or lets the repair bypass the normal source and semantic checks.
    """
    indexed = {fact['fact_id']: fact for fact in facts}
    catalog = {key: {'text': fact['text'] + '\n' + '\n'.join(c['quote'] for c in fact['citations'])}
               for key, fact in indexed.items()}
    feedback = []
    for statement in statements(draft):
        if statement['statement_id'] not in targets:
            continue
        bound = [indexed[key] for key in statement.get('fact_ids', []) if key in indexed]
        support = '\n'.join(fact['text'] + '\n' + '\n'.join(c['quote'] for c in fact['citations']) for fact in bound)
        missing = presentation.presentation_numbers(statement['text']) - presentation.presentation_numbers(support, chinese_counts=True)
        if missing:
            hints = presentation.numeric_source_hints(statement['text'],
                [{'ref': key} for key in statement.get('fact_ids', [])], catalog, limit=5)
            feedback.append({'statement_id': statement['statement_id'],
                'unsupported_numbers': sorted(missing),
                'bound_sources': [{'fact_id': fact['fact_id'], 'text': fact['text'],
                                   'quotes': [citation['quote'] for citation in fact['citations']]} for fact in bound],
                'candidate_sources': [indexed[key] for key in hints['candidate_refs']],
                'repair_action': ('select_supported_fact_or_remove_number' if hints['candidate_refs']
                                  else 'remove_unbound_number'),
                'candidate_policy': '同项目已核对事实的检索候选，不表示语义已成立；最终独立审核必须检查实际绑定。'})
    return feedback


def repair_input_context(context, *, numeric_only=False):
    """A numeric-only editor must use checked facts, not unselected page text.

    The full original pages remain available to the subsequent independent
    material audit for limits and conflicting statements. They cannot enlarge
    the fact catalog just because a paragraph points at the same page.
    """
    if not numeric_only:
        return context
    return {key: context[key] for key in ('project_id', 'project_name', 'checked_facts') if key in context}


def repair_numeric_bindings(draft, facts, program_errors, request):
    """Repair source selections before rewriting otherwise valid paragraphs."""
    targets = {entry['statement_id'] for entry in program_errors if entry.get('reason') == 'project_number_without_source'}
    feedback = [entry for entry in numeric_repair_feedback(draft, targets, facts) if entry['candidate_sources']]
    if not feedback:
        return draft, program_errors
    selected = {entry['statement_id']: entry for entry in feedback}
    current = {value['statement_id']: value for value in statements(draft)}
    raw = request('expression-bindings', prompts.BINDINGS,
        {'statements': [current[key] for key in selected], 'citation_feedback': feedback}, {})
    groups = {}
    for entry in raw.get('bindings', []):
        if isinstance(entry, dict) and entry.get('statement_id') in selected:
            groups.setdefault(entry['statement_id'], []).append(entry)
    changes = []
    for key, entries in groups.items():
        if len(entries) != 1:
            continue
        additions = entries[0].get('add_fact_ids')
        allowed = {fact['fact_id'] for fact in selected[key]['candidate_sources']}
        if not isinstance(additions, list) or any(not isinstance(value, str) or value not in allowed for value in additions):
            continue
        value = current[key]
        keys = list(dict.fromkeys(value['fact_ids'] + additions))
        if keys != value['fact_ids']:
            changes.append({**value, 'fact_ids': keys})
    if not changes:
        return draft, program_errors
    result = apply_changes(draft, {'changes': changes}, {value['statement_id'] for value in changes}, facts, allow_remaining=True)
    return normalize(result, facts)


def complete_material_audit(draft, context, request, settings, attempt, program_errors):
    values = statements(draft); expected = [value['statement_id'] for value in values]
    audit = request(f'expression-audit-{attempt}', prompts.AUDIT,
        {**context, 'draft': draft, 'draft_statements': values, 'program_feedback': program_errors}, settings)
    record = {'draft_sha256': contract.digest(draft), 'answer': audit}
    targets = audit_protocol.coverage_targets(audit, expected)
    if targets:
        scoped = [value for value in values if value['statement_id'] in targets]
        supplemental = request(f'expression-audit-{attempt}-coverage', prompts.AUDIT_COVERAGE,
            {**context, 'draft': draft, 'draft_statements': scoped,
             'coverage_expected_statement_ids': [value['statement_id'] for value in scoped],
             'base_audit_sha256': contract.digest(audit)}, settings)
        record['coverage_answer'] = supplemental
        record['coverage_statement_ids'] = [value['statement_id'] for value in scoped]
        # Missing coverage is a protocol failure, not a claim that the report
        # is wrong. Retain every explicit rejection from the original audit.
        if audit_protocol.coverage_targets(supplemental, [value['statement_id'] for value in scoped]):
            record['protocol_incomplete'] = True
            return audit, record
        audit = audit_protocol.merge_coverage(audit, supplemental, expected, targets)
        record['answer'] = audit
    if audit_protocol.coverage_targets(audit, expected):
        record['protocol_incomplete'] = True
    return audit, record


def material_audit_provenance(record, draft, facts, root):
    """Reconstruct completed coverage from actual request/response files."""
    errors, requests = [], []
    expected = [value['statement_id'] for value in statements(draft)]

    def read(path):
        selected = (root / path).resolve()
        if not selected.is_relative_to((root / 'runs').resolve()):
            raise ValueError('Audit request outside private runs')
        return json.loads(selected.read_text())

    primary = read(record['cache_file']); payload = primary['request']; requests.append(payload)
    if (payload.get('draft') != draft or payload.get('draft_statements') != statements(draft)
            or payload.get('checked_facts') != facts):
        errors.append('audit_not_bound_to_final_text')
    answer = primary['answer']; targets = audit_protocol.coverage_targets(answer, expected)
    repairs = record.get('coverage_repairs', [])
    if repairs:
        if len(repairs) != 1 or not targets:
            return errors + ['invalid_audit_coverage_provenance'], requests
        entry = repairs[0]; item = read(entry['cache_file']); payload = item['request']; requests.append(payload)
        ids = [key for key in expected if key in targets]
        values = [value for value in statements(draft) if value['statement_id'] in targets]
        if (entry.get('statement_ids') != ids or payload.get('coverage_expected_statement_ids') != ids
                or payload.get('draft') != draft or payload.get('draft_statements') != values
                or payload.get('checked_facts') != facts or item.get('answer') != entry.get('answer')
                or payload.get('base_audit_sha256') != contract.digest(answer)
                or audit_protocol.coverage_targets(item.get('answer', {}), ids)):
            errors.append('audit_coverage_not_bound_to_final_text')
        answer = audit_protocol.merge_coverage(answer, item['answer'], expected, targets)
    if record.get('answer') != answer:
        errors.append('audit_response_provenance_mismatch')
    if audit_protocol.coverage_targets(answer, expected):
        errors.append('audit_protocol_incomplete')
    return errors, requests


def review_context(path, source):
    if path is None: return None, None
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT / 'runs'): raise ValueError('Review snapshot must be inside runs')
    report = json.loads(path.read_text())
    if report['project_id'] != source['project_id'] or any(report['review_scope'][r + '_sha256'] != source[r + '_sha256'] for r in ('plan', 'report')):
        raise ValueError('Paired review is not based on the same source pair')
    return contract.render_markdown(report).split('## 附录：', 1)[0], {
        'review_report_file': str(path.relative_to(ROOT)), 'review_report_sha256': base.sha256(path)}


def run_project(pair, run, output, config, execute, facts_from=None, review_path=None):
    started = time.monotonic(); run, output = Path(run), Path(output)
    if not run.resolve().is_relative_to(ROOT / 'runs'): raise ValueError('Private run must be inside runs')
    with call_scope(report='achievement', stage='extract'): source = sources.extract(pair)
    pid = pair.project_id; legacy.write_json(run / 'sources' / f'{pid}.json', source)
    facts, fact_audit, calls, reused = prepare_facts(source, run, config, execute, facts_from)
    catalog = {'facts': facts, 'audit': fact_audit, 'calls': copy.deepcopy(calls), 'reuse_proof': reused}
    legacy.write_json(run / 'facts' / f'{pid}.json', catalog)
    context = {'project_id': pid, 'project_name': source['project_name'], 'framework_method': legacy.framework()['method_only'],
        'checked_facts': facts, 'fact_source_context': {c['ref']: source['records'][c['ref']] for f in facts for c in f['citations']}}
    paired, paired_proof = review_context(review_path, source)
    if paired: context['paired_review'] = paired
    # Long-form generation gets its own bounded budget; fulfillment timeouts are unchanged.
    writer_config = {**config, 'request_timeout_seconds': 240, 'request_total_budget_seconds': 240, 'request_attempts': 1}
    audit_config = {**config, 'request_timeout_seconds': 120, 'request_total_budget_seconds': 120, 'request_attempts': 1}
    def request(stage, system, payload, settings):
        with call_scope(report='achievement', stage=stage):
            answer, usage = legacy.cached_call(run, pid, stage, system, payload, settings or writer_config, execute)
        calls.append({'stage': stage, **usage}); return answer
    raw = request('expression-write', prompts.WRITER, context, writer_config)
    draft, program_errors = normalize(raw, facts)
    if program_errors:
        draft, program_errors = repair_numeric_bindings(draft, facts, program_errors, request)
    audits = []
    for attempt in range(2):
        start = len(calls)
        audit, record = complete_material_audit(draft, context, request, audit_config, attempt, program_errors)
        record['cache_file'] = calls[start]['cache_file']
        if 'coverage_answer' in record:
            record['coverage_repairs'] = [{'answer': record.pop('coverage_answer'),
                'statement_ids': record.pop('coverage_statement_ids'), 'cache_file': calls[start + 1]['cache_file']}]
        audits.append(record)
        if record.get('protocol_incomplete'):
            legacy.write_json(run / 'revision-records' / f'{pid}-audit-protocol-{attempt}.json', record)
            raise ValueError('Expression audit response incomplete after coverage repair')
        errors = program_errors + audit_errors(draft, audit, bool(paired))
        if not errors: break
        feedback_file = run / 'revision-records' / f'{pid}-{attempt}.json'
        legacy.write_json(feedback_file, {'draft': draft, 'errors': errors, 'audit': audit})
        if attempt:
            raise contract.ReportValidationError('Expression material-fact check failed after one targeted repair',
                                                 stage='achievement/facts', feedback_file=feedback_file)
        targets = {e['statement_id'] for e in program_errors}
        all_ids = [s['statement_id'] for s in statements(draft)]
        material_targets = audit_protocol.content_targets(audit, all_ids)
        targets.update(material_targets)
        if not targets or not targets <= set(all_ids):
            raise ValueError('Expression material conflict lacks a valid statement location')
        ordered = [key for key in all_ids if key in targets]
        # Bound each response by paragraph count, rather than rejecting an
        # otherwise repairable report merely for having more than 12 issues.
        for offset in range(0, len(ordered), 6):
            chunk = set(ordered[offset:offset + 6])
            stage = 'expression-repair' if len(ordered) <= 6 else f'expression-repair-part-{offset // 6 + 1:02d}'
            patch = request(stage, prompts.REPAIR,
                {**repair_input_context(context, numeric_only=not bool(chunk & material_targets)),
                 'draft': draft, 'targets': sorted(chunk), 'feedback': errors,
                 'numeric_feedback': numeric_repair_feedback(draft, chunk, facts)}, writer_config)
            before = {value['statement_id']: (value['text'], value.get('fact_ids')) for value in statements(draft)}
            draft = apply_changes(draft, patch, chunk, facts, allow_remaining=True)
            after = {value['statement_id']: (value['text'], value.get('fact_ids')) for value in statements(draft)}
            if any(before[key] == after[key] for key in chunk & material_targets):
                raise ValueError('Expression repair left a rejected statement unchanged')
        draft, program_errors = normalize(draft, facts)
        if program_errors:
            draft, program_errors = repair_numeric_bindings(draft, facts, program_errors, request)
        if program_errors:
            residual = {error['statement_id'] for error in program_errors}
            for offset in range(0, len(residual), 6):
                chunk = set(sorted(residual)[offset:offset + 6])
                patch = request(f'expression-repair-numeric-{offset // 6 + 1:02d}', prompts.REPAIR,
                    {**repair_input_context(context, numeric_only=True),
                     'draft': draft, 'targets': sorted(chunk), 'feedback': program_errors,
                     'previous_repair_failed': {'statement_ids': sorted(chunk),
                         'instruction': '上一轮修订仍保留未绑定数值。本轮为最后一次定点修订，请按numeric_feedback移除这些数值或补选真正支持的事实，不能原样返回。'},
                     'numeric_feedback': numeric_repair_feedback(draft, chunk, facts)}, writer_config)
                draft = apply_changes(draft, patch, chunk, facts, allow_remaining=True)
            draft, program_errors = normalize(draft, facts)
            if program_errors:
                legacy.write_json(run / 'revision-records' / f'{pid}-source-repair.json',
                    {'draft': draft, 'errors': program_errors, 'feedback': numeric_repair_feedback(draft, residual, facts)})
                raise ValueError('Expression source binding or numeric repair incomplete')
    validation = {'status': 'passed', 'facts_audit': fact_audit, 'material_audits': audits,
                  'draft_sha256': contract.digest(draft), 'fact_reuse': reused}
    if paired_proof:
        validation['paired_report_consistency'] = {**paired_proof, 'passed': True,
            'final_draft_sha256': contract.digest(draft), 'checked_in': 'material_audit'}
    report = {'schema_version': VERSION, 'project_id': pid, 'source': source, 'facts': facts, 'draft': draft,
        'validation': validation, 'run_metadata': {'model': config['text_model'], 'provider': config.get('provider', 'siliconflow'),
            'prompt_version': prompts.VERSION, 'framework': legacy.framework(), 'calls': calls,
            'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'input_files': [source['plan_file'], source['report_file']],
            'external_retrieval': False, 'validated_facts_reused': bool(reused), 'wall_seconds': round(time.monotonic() - started, 2)}}
    # Validate before publishing any report or rendering, even in the standalone CLI.
    errors = check(report, render(report), ROOT, require_paired=bool(paired))
    if errors: raise ValueError('Expression export check failed: ' + str(errors))
    save(report, run, output)
    return {'project_id': pid, 'project_name': source['project_name'], 'filename': legacy.filename(source['project_name']),
        'facts': len(facts), 'calls': len(calls), 'facts_reused': bool(reused), 'wall_seconds': report['run_metadata']['wall_seconds']}


def save(report, run, output):
    legacy.write_json(Path(run) / 'reports' / f"{report['project_id']}.json", report)
    Path(output).mkdir(parents=True, exist_ok=True)
    (Path(output) / legacy.filename(report['source']['project_name'])).write_text(render(report))


def export_reader_index(run, output, results):
    entries = [json.loads((Path(run) / 'reports' / f"{r['project_id']}.json").read_text()) for r in results]
    lines = ['# 成果表达试稿｜v0.3', '',
        '本版改用“六问事实表＋简版＋通俗详述＋集中来源”结构。履约报告沿用既有版本，未重新生成。', '',
        '输入仅为本项目任务书与中期总结报告；未加入额外研究材料或外部检索。', '',
        '| 项目 | 简版字数 | 详述字数 | 阅读报告 |', '| --- | ---: | ---: | --- |']
    for report in sorted(entries, key=lambda r: r['project_id']):
        draft = report['draft']; short = sum(len(p['text']) for p in draft['short_version'])
        long = sum(len(p['text']) for s in draft['long_sections'] for p in s['paragraphs'])
        name = report['source']['project_name']
        lines.append(f'| {contract.cell(name)} | {short} | {long} | [打开报告]({quote(legacy.filename(name))}) |')
    models = '、'.join(sorted({r['run_metadata']['model'] for r in entries}))
    lines += ['', '模型：' + models + '。字数按正文字符统计，不含事实表、标题和附录。', '',
        '正文集中解释成果与价值；内部事实绑定、完整原文及模型复核记录保存在私有运行目录。来源附录可直接回查。', '',
        '通俗详述目标随材料密度调整，本轮两个试稿约两千字，不为达到模板篇幅重复背景。', '',
        '本轮先复用已核对事实，写作后做独立关键事实检查。实质问题最多定点修订一次，仍不通过则不导出；表达建议不触发整篇重写。', '']
    Path(output).mkdir(parents=True, exist_ok=True)
    (Path(output) / 'README.md').write_text('\n'.join(lines))


def check_paired(report, run, output, review_path, config, execute):
    text, proof = review_context(review_path, report['source'])
    previous = report['validation'].get('paired_report_consistency', {})
    if (previous.get('passed') is True and previous.get('final_draft_sha256') == contract.digest(report['draft'])
        and all(previous.get(k) == v for k, v in proof.items())): return
    answer, usage = legacy.cached_call(run, report['project_id'], 'expression-paired', prompts.PAIRED,
        {**legacy.checked_context(report), 'paired_review': text, 'achievement_draft': report['draft']}, config, execute)
    if answer.get('consistent') is not True or answer.get('issues') != []:
        legacy.write_json(Path(run) / 'paired-conflict' / f"{report['project_id']}.json", answer)
        raise ValueError('Achievement expression and fulfillment report have a material conflict')
    report['run_metadata']['calls'].append({'stage': 'expression-paired', **usage})
    report['validation']['paired_report_consistency'] = {**proof, 'passed': True, 'audit': answer,
        'cache_file': usage['cache_file'], 'final_draft_sha256': contract.digest(report['draft'])}
    save(report, run, output)


def render(report):
    source, draft = report['source'], report['draft']; pid = report['project_id'].lower()
    prefix = 'achievement-' + pid + '-'
    used = {key for item in statements(draft) for key in item['fact_ids']}
    facts = [f for f in report['facts'] if f['fact_id'] in used]
    refs = list(dict.fromkeys(c['ref'] for f in facts for c in f['citations']))
    lines = ['# 项目阶段成果与后续推进分析报告', '', '**项目：' + source['project_name'] + '**', '']
    lines += profile.header_lines(source['administrative_profile'])
    lines += ['信息来源：本项目任务书与中期总结报告。', '',
        '- 任务书：' + Path(source['plan_file']).name, '- 总结报告：' + Path(source['report_file']).name, '',
        '[查看资料来源](#' + prefix + 'sources)', '', '## 一、事实成果提炼', '',
        '| 核心问题 | 事实提炼 |', '| --- | --- |']
    for key, label in QUESTIONS.items(): lines.append('| ' + label + ' | ' + contract.cell(draft['fact_sheet'][key]['text']) + ' |')
    lines += ['', '## 二、成果表达', '', '### 简版', '', '**' + draft['value_title'] + '**', '']
    for value in draft['short_version']: lines += [value['text'], '']
    lines += ['### ' + ('科普长版' if draft.get('narrative_format') else '通俗详述'), '']
    for value in draft.get('long_intro', []): lines += [value['text'], '']
    for section in draft['long_sections']:
        lines += ['#### ' + section['title'], '']
        for value in section['paragraphs']: lines += [value['text'], '']
    lines += ['## 附录：成果来源与必要说明', '', f'<a name="{prefix}sources" id="{prefix}sources"></a>', '']
    if draft.get('questions'):
        lines += ['### 表达中仍需明确的事项', '']
        for value in draft['questions']: lines += [value['text'], '']
    lines += ['### 关键事实与来源', '', '| 事实主题 | 已核对的事实 | 材料位置 |', '| --- | --- | --- |']
    for fact in facts:
        links = '、'.join(f'[来源{refs.index(c["ref"]) + 1:02d}](#{prefix}{refs.index(c["ref"]) + 1:02d})' for c in fact['citations'])
        lines.append('| ' + contract.cell(fact['title']) + ' | ' + contract.cell(fact['text']) + ' | ' + links + ' |')
    lines += ['', '### 来源摘录', '']
    if source.get('scanned_report'): lines += ['总结报告摘录来自本机OCR，位置按原PDF文件页码标注。', '']
    for i, ref in enumerate(refs, 1):
        record = source['records'][ref]
        quotes = list(dict.fromkeys(c['quote'] for f in facts for c in f['citations'] if c['ref'] == ref))
        # Excerpts are literal; full original context remains in JSON. Avoid
        # reproducing the entire taskbook repeatedly inside the reading copy.
        lines += [f'<a name="{prefix}{i:02d}" id="{prefix}{i:02d}"></a>', '',
            f'#### 来源{i:02d}｜' + ('任务书' if record['side'] == 'plan' else '总结报告'), '',
            f"文件：{Path(record['file']).name}；位置：{record['location']}；栏目：{record['section']}。", '']
        for quote in quotes: lines += ['> ' + line for line in quote.splitlines()] + ['']
    return '\n'.join(lines)


def check(report, markdown, root=ROOT, require_paired=False):
    errors = []; source = report['source']; v = report['validation']; draft = report['draft']
    expected = {source['plan_file'], source['report_file']}
    if len(expected) != 2 or set(report['run_metadata']['input_files']) != expected or any(r['file'] not in expected for r in source['records'].values()):
        errors.append('not_exactly_two_paired_inputs')
    for role in ('plan', 'report'):
        if base.sha256(root / source[role + '_file']) != source[role + '_sha256']: errors.append('source_hash_changed:' + role)
    fresh = sources.extract(base.Pair(report['project_id'], root / source['plan_file'], root / source['report_file']))
    if fresh != source: errors.append('source_sections_changed')
    _, fact_errors = legacy.resolve_facts({'facts': report['facts']}, source['records']); errors += fact_errors
    errors += legacy.audit_checks(v['facts_audit'], 'fact_id', [f['fact_id'] for f in report['facts']], FACT_FIELDS)
    if v.get('fact_reuse'):
        proof = v['fact_reuse']
        for kind in ('catalog', 'source', 'audit'):
            if base.sha256(root / proof[kind + '_file']) != proof[kind + '_sha256']: errors.append('fact_provenance_changed:' + kind)
        facts, audit, _ = read_reusable_facts(root / proof['catalog_file'], source)
        if facts != report['facts'] or audit != v['facts_audit']: errors.append('reused_facts_changed')
    normalized, structural = normalize(draft, report['facts']); errors += structural
    if normalized != draft: errors.append('statement_ids_changed')
    if v.get('status') != 'passed' or v.get('draft_sha256') != contract.digest(draft): errors.append('draft_changed_since_check')
    final = v.get('material_audits', [{}])[-1]
    if final.get('draft_sha256') != contract.digest(draft): errors.append('audit_draft_changed')
    provenance_errors, audit_requests = material_audit_provenance(final, draft, report['facts'], root)
    errors += provenance_errors
    errors += audit_errors(draft, final['answer'], paired=require_paired and v.get('paired_report_consistency', {}).get('checked_in') == 'material_audit')
    if require_paired:
        paired = v.get('paired_report_consistency', {})
        if (paired.get('passed') is not True or paired.get('final_draft_sha256') != contract.digest(draft) or
            base.sha256(root / paired['review_report_file']) != paired['review_report_sha256']): errors.append('paired_consistency_outdated')
        if paired.get('checked_in') == 'material_audit':
            text, _ = review_context(root / paired['review_report_file'], source)
            if any(payload.get('paired_review') != text for payload in audit_requests):
                errors.append('paired_audit_context_changed')
        elif paired.get('checked_in') == 'grouped_review':
            from project_report_grouped import validate_paired_proof
            try:
                reviewed = json.loads((root / paired['review_report_file']).read_text())
                validate_paired_proof(reviewed,report,root)
                if any(paired.get(key) != value for key,value in reviewed['validation']['grouped_paired'].items()):
                    errors.append('grouped_pair_proof_changed')
            except (ValueError,KeyError,TypeError,OSError):
                errors.append('grouped_pair_audit_not_bound')
        elif paired.get('cache_file'):
            cross = json.loads((root / paired['cache_file']).read_text())
            text, _ = review_context(root / paired['review_report_file'], source)
            if cross['request'].get('achievement_draft') != draft or cross['request'].get('paired_review') != text or cross.get('answer') != paired['audit']:
                errors.append('paired_audit_not_bound')
            if paired['audit'].get('consistent') is not True or paired['audit'].get('issues') != []: errors.append('paired_material_conflict')
        else: errors.append('paired_audit_missing')
    if render(report) != markdown: errors.append('markdown_not_reproducible')
    from feishu_report_bundle import check_links
    try: check_links(markdown)
    except ValueError: errors.append('broken_markdown_links')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-snapshot', type=Path, required=True, help='Existing extraction containing the two original paths')
    parser.add_argument('--facts-from', type=Path)
    parser.add_argument('--review-report', type=Path)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(); old = json.loads(args.source_snapshot.read_text())
    pair = base.Pair(old['project_id'], ROOT / old['plan_file'], ROOT / old['report_file'])
    if any(base.sha256(getattr(pair, r + '_path')) != old[r + '_sha256'] for r in ('plan', 'report')):
        parser.error('Original source files changed')
    result = run_project(pair, args.run_dir.resolve(), args.output_dir.resolve(), model_config(), args.execute, args.facts_from, args.review_report)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__': main()
