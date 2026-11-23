"""Turn checked differences into source-bound topics for manager–PI discussions."""
from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path

import project_report_contract as c
import project_report_presentation as p

VERSION = 'project-pi-coordination-v0.4.1'


def targets_for(report):
    items = {item['item_id']: item for item in report['plan_items']}
    targets = []
    for row in report['alignments']:
        if not row.get('semantic_reviewed') or row.get('validation_issues'):
            continue
        current = [cite for cite in row.get('evidence', []) if cite['role'] == 'current']
        if row['status'] in {'部分完成', '尚未完成'}:
            kind = 'confirmed_difference'
        elif row.get('attention'):
            kind = 'stage_to_confirm'
        elif (row.get('confirmation_kind') == 'business'
              and any(x.get('rule') == 'completion_and_progress_conflict' for x in row.get('semantic_overrides', []))
              and len(current) >= 2):
            kind = 'conflicting_statements'
        else:
            continue
        if not current:
            continue
        item = items[row['item_id']]
        targets.append({'item_id': row['item_id'], 'kind': kind, 'plan': item['quote'],
                        'source_ref': item['source_ref'], 'due': item['due'],
                        'status': row['status'], 'progress': row['progress'], 'reason': row['reason'],
                        'question': row.get('question', ''), 'overdue': bool(row.get('overdue')),
                        'evidence': current, 'next_steps': row.get('next_steps', [])})
    return targets


def context_for(report, source, records, targets):
    plan_refs = list(dict.fromkeys(item['source_ref'] for item in targets))
    report_refs = {cite['ref'] for item in targets for cite in item['evidence'] + item['next_steps']}
    reduced_source = {**source, 'plan_blocks': {ref: source['plan_blocks'][ref] for ref in plan_refs}}
    reduced_records = {ref: value for ref, value in records.items() if ref in report_refs}
    context = {'project_name': report['project_profile']['project_name'],
               'approved_topics': targets,
               'plan_sources': [{'ref': ref, 'text': text} for ref, text in reduced_source['plan_blocks'].items()],
               'report_sources': [{'ref': ref, **value} for ref, value in reduced_records.items()]}
    return context, reduced_source, reduced_records


def attach_checked_topics(report, source, records):
    """Route checked alignments to the final finding writer without paraphrasing.

    These are internal seeds, not new semantic audit decisions. Final finding
    text is still independently audited against both source sides.
    """
    targets = targets_for(report)
    items = []
    for index, target in enumerate(targets, 1):
        citations = [{'ref': target['source_ref'], 'quote': target['plan'], 'role': 'target', 'side': 'plan'}]
        citations += [copy.deepcopy(cite) | {'side': 'report'} for cite in target['evidence'] + target['next_steps']]
        for cite in citations:
            raw = source['plan_blocks'].get(cite['ref'], '') if cite['side'] == 'plan' else records.get(cite['ref'], {}).get('text', '')
            if not c.literal_quote(cite['quote'], raw):
                raise ValueError('Checked topic source no longer matches input')
        items.append({'discussion_id': f'P{index:02d}', 'title': p.label({'quote': target['plan']}),
            'item_ids': [target['item_id']], 'difference': target['reason'],
            'discuss_with_pi': target['question'] or '请老师说明该项工作的当前进展及后续安排。',
            'citations': citations})
    result = {'version': VERSION, 'mode': 'checked_alignment_projection', 'items': items,
        'target_item_ids': [value['item_id'] for value in targets], 'missing_item_ids': [],
        'source_alignment_digest': c.digest(targets)}
    attach(report, result, [])


def resolve_sources(raw, source, records):
    draft = copy.deepcopy(raw)
    for index, item in enumerate(draft.get('items', []), 1):
        if not isinstance(item, dict):
            continue
        item['discussion_id'] = f'P{index:02d}'
        for cite in item.get('citations', []):
            if isinstance(cite, dict) and 'quote' not in cite:
                cite['quote'] = source['plan_blocks'].get(cite.get('ref'), records.get(cite.get('ref'), {}).get('text', ''))
    return draft


def validate(raw, audit, targets, source, records):
    target_map = {item['item_id']: item for item in targets}
    checks = {}
    for check in audit.get('checks', []):
        if isinstance(check, dict):
            checks.setdefault(check.get('discussion_id'), []).append(check)
    accepted, rejected, used = [], [], Counter()
    for raw_item in raw.get('items', []):
        if not isinstance(raw_item, dict):
            rejected.append({'errors': ['invalid_coordination_shape']})
            continue
        item = copy.deepcopy(raw_item)
        errors = []
        linked = item.get('item_ids')
        if not isinstance(linked, list) or not linked or any(key not in target_map for key in linked) or len(linked) != len(set(linked)):
            errors.append('unapproved_coordination_topic')
            linked = []
        citations = []
        for raw_cite in item.get('citations', []):
            cite, error = p.checked_display_citation(raw_cite, source, records)
            if error:
                errors.append(error)
            else:
                citations.append(cite)
        allowed_plan = {target_map[key]['source_ref'] for key in linked}
        allowed_report = {cite['ref'] for key in linked for cite in target_map[key]['evidence'] + target_map[key]['next_steps']}
        if any(cite['ref'] not in allowed_plan | allowed_report for cite in citations):
            errors.append('citation_outside_linked_topic')
        if not any(cite['side'] == 'plan' for cite in citations) or not any(cite['role'] == 'current' for cite in citations):
            errors.append('coordination_missing_two_sided_sources')
        fields = ('title', 'difference', 'discuss_with_pi')
        if any(not isinstance(item.get(field), str) or not item[field].strip() for field in fields):
            errors.append('incomplete_coordination_text')
        text = ' '.join(str(item.get(field, '')) for field in fields)
        support = ' '.join(cite['quote'] for cite in citations)
        if not p.number_support(text, support):
            errors.append('unsupported_coordination_number')
        if c.asserts_delay(text) and not all(target_map[key]['overdue'] for key in linked):
            errors.append('unsupported_coordination_delay')
        if any(target_map[key]['kind'] == 'stage_to_confirm' for key in linked):
            if re.search(r'中期.{0,8}(?:未达|不达标|未完成|不满足|差距|不足)|(?:未达|不满足).{0,8}中期', text):
                errors.append('stage_question_upgraded_to_breach')
        if any(target_map[key]['kind'] == 'conflicting_statements' for key in linked):
            if not (re.search(r'已.{0,12}完成|完成.{0,15}验证|已.{0,12}验证', str(item.get('difference', '')))
                    and re.search(r'推进|正在|待发布|尚待|仍在', str(item.get('difference', '')))):
                errors.append('conflicting_statements_not_both_retained')
        if re.search(r'违约|追责|罚款|通过验收|(?:项目|综合|审查).{0,5}(?:评分|得分)|真实性|涉嫌造假|校验降级|模型输出|技术复核', text):
            errors.append('unsupported_coordination_scope_or_rating')
        checks_for_item = checks.get(item.get('discussion_id'), [])
        if len(checks_for_item) != 1 or checks_for_item[0].get('supported') is not True:
            errors.append('coordination_semantic_audit_failed')
        if any(used[key] for key in linked):
            errors.append('duplicate_coordination_topic')
        if errors:
            rejected.append({'item': raw_item, 'errors': sorted(set(errors)), 'audit': checks_for_item})
        else:
            item['citations'] = citations
            item['item_ids'] = linked
            accepted.append(item)
            used.update(linked)
    missing = [key for key in target_map if used[key] != 1]
    return {'version': VERSION, 'items': accepted, 'target_item_ids': list(target_map), 'missing_item_ids': missing}, rejected


def attach(report, result, rejected):
    report['presentation']['pi_coordination'] = result
    report['validation']['coordination_rejections'] = rejected
    index = report['source_index']
    lookup = {(entry['ref'], entry['quote']) for entry in index}
    for item in result['items']:
        for cite in item['citations']:
            key = (cite['ref'], cite['quote'])
            if key in lookup:
                continue
            lookup.add(key)
            index.append({'source_id': f'依据{len(index) + 1:02d}', 'side': cite['side'], 'ref': cite['ref'],
                          'file_name': Path(report['review_scope']['plan_file' if cite['side'] == 'plan' else 'report_file']).name,
                          'location': c.location(cite['ref']), 'quote': cite['quote']})
