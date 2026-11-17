"""Source-bound acceptance-basis checks and task-led follow-up arrangements."""
from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path

import project_report_contract as c
import project_report_presentation as p

VERSION = 'project-report-assurance-v0.5.0'
BASIS_STATUSES = {'依据对应', '口径待确认', '存在口径出入'}
SCOPES = {'本期适用', '适用范围待确认'}


def citation_candidates(draft, records):
    """Retrieve possible sources for unsupported actual numbers, not judgements."""
    requests = []
    for item in draft.get('items', []):
        if not isinstance(item, dict) or not isinstance(item.get('reported'), dict):
            continue
        reported = ' '.join(str(value) for value in item['reported'].values())
        commentary = ' '.join(str(item.get(field) or '') for field in ('assessment','question','topic_title'))
        text = reported + ' ' + commentary
        selected = ' '.join(cite.get('quote', '') for cite in item.get('citations', []) if cite.get('role') == 'current')
        planned = ' '.join(cite.get('quote','') for cite in item.get('citations',[]) if cite.get('role') == 'target')
        missing = (p.presentation_numbers(reported) - p.presentation_numbers(selected, chinese_counts=True))
        missing |= p.presentation_numbers(commentary) - p.presentation_numbers(planned + ' ' + selected, chinese_counts=True)
        if not missing:
            continue
        bigrams = {text[index:index + 2] for index in range(len(text) - 1)
                   if all('\u4e00' <= char <= '\u9fff' for char in text[index:index + 2])}
        ranked = []
        for ref, record in records.items():
            if record['role'] not in {'actual_or_body', 'summary_and_next'}:
                continue
            overlap = missing & p.presentation_numbers(record['text'], chinese_counts=True)
            score = sum(token in record['text'] for token in bigrams)
            if overlap and score:
                ranked.append((len(overlap), score, ref, record))
        ranked.sort(key=lambda value: (-value[0], -value[1], value[2]))
        if ranked:
            requests.append({'item_id': item['item_id'], 'fixed_reported': item['reported'],
                             'fixed_expected': item['expected'], 'existing_citations': item['citations'],
                             'fixed_assessment': item.get('assessment'), 'fixed_question': item.get('question'),
                             'fixed_topic_title': item.get('topic_title'),
                             'unsupported_number_tokens': sorted(missing),
                             'candidate_sources': [{'ref': ref, **record} for _, _, ref, record in ranked[:6]]})
    return requests


def add_selected_citations(draft, patch, requests, records):
    result = copy.deepcopy(draft)
    allowed = {item['item_id']: {value['ref'] for value in item['candidate_sources']} for item in requests}
    by_id = {item['item_id']: item for item in result.get('items', []) if isinstance(item, dict)}
    for addition in patch.get('additions', []):
        item = by_id.get(addition.get('item_id'))
        if not item:
            continue
        for ref in addition.get('additional_refs', []):
            if ref not in allowed.get(addition['item_id'], set()) or any(cite.get('ref') == ref for cite in item['citations']):
                continue
            cite, error = c.checked_citation({'ref': ref, 'quote': records[ref]['text'], 'role': 'current'}, records)
            if not error:
                item['citations'].append(cite)
    return result


def deliverable_state_context(draft, records, plan_items=()):
    requests = []
    original = {item['item_id']: item['quote'] for item in plan_items}
    for item in draft.get('items', []):
        if not isinstance(item, dict):
            continue
        expected = ' '.join(str(value) for value in (item.get('expected') or {}).values())
        expected += ' ' + original.get(item['item_id'], '')
        reported = ' '.join(str(value) for value in (item.get('reported') or {}).values())
        keyword = None
        if '专利申请' in expected and '交底书' in reported:
            keyword = '专利'
        elif '发表' in expected and not re.search(r'投稿|录用|录取', expected) and re.search(r'投稿|录用|录取', reported):
            keyword = '论文'
        if keyword:
            candidates = [{'ref': ref, **record} for ref, record in records.items()
                          if record['role'] in {'actual_or_body', 'summary_and_next'} and keyword in record['text']]
            requests.append({'fixed_item_id': item['item_id'], 'original_plan_quote': original.get(item['item_id'], ''),
                             'draft_item': item, 'report_sources': candidates})
    return requests


def apply_state_review(draft, reviewed, requests):
    result = copy.deepcopy(draft)
    allowed = {request['fixed_item_id'] for request in requests}
    changes = {}
    for item in reviewed.get('items', []):
        if isinstance(item, dict) and item.get('item_id') in allowed:
            changes.setdefault(item['item_id'], []).append(item)
    for index, item in enumerate(result.get('items', [])):
        replacements = changes.get(item.get('item_id'), [])
        if len(replacements) == 1:
            result['items'][index] = replacements[0]
    return result


def acceptance_context(report, source, records):
    visible_ids = {row['item_id'] for row in report['alignments'] if p.visible_alignment(row)}
    items = [item for item in report['plan_items'] if item['item_id'] in visible_ids]
    refs = list(dict.fromkeys(item['source_ref'] for item in items))
    if 'P-CORE' in source['plan_blocks'] and 'P-CORE' not in refs:
        refs.append('P-CORE')
    return {'project_name': report['project_profile']['project_name'],
            'review_stage': source['review_stage'], 'checkpoint_date': source['checkpoint_date'],
            'plan_items': items, 'checked_alignments': [row for row in report['alignments'] if row['item_id'] in visible_ids],
            'plan_sources': [{'ref': ref, 'text': source['plan_blocks'][ref]} for ref in refs],
            'report_sources': [{'ref': ref, **value} for ref, value in records.items()]}


def resolve(raw, source, records, field, prefix, with_summary=False):
    result = copy.deepcopy(raw)
    for index, item in enumerate(result.get(field, []), 1):
        if isinstance(item, dict):
            item['check_id'] = f'{prefix}{index:02d}'
    values = list(result.get(field, []))
    if with_summary and isinstance(result.get('summary'), dict):
        result['summary']['check_id'] = 'AS01'
        values.append(result['summary'])
    known_ids = {item.get('item_id') for item in values if isinstance(item, dict) and item.get('item_id')}
    if isinstance(result.get('summary'), dict):
        known_ids.update(result['summary'].get('item_ids', []))
    for value in values:
        if not isinstance(value, dict):
            continue
        # References in parenthetical annotations are metadata, not measured
        # numbers. Move explicitly selected source IDs to citations before the
        # independent audit, retaining the facts and all substantive numbers.
        annotation_refs = []
        def clean_text(text):
            if not isinstance(text, str):
                return text
            def annotation(match):
                tokens = [token for token in re.split(r'[,，、;；\s]+', match.group(1).strip()) if token]
                if tokens and all(token in source['plan_blocks'] or token in records or token in known_ids for token in tokens):
                    annotation_refs.extend(token for token in tokens if token not in known_ids)
                    return ''
                return match.group()
            return re.sub(r'[（(]([^（）()]+)[）)]', annotation, text)
        for key in ('assessment', 'question', 'topic_title', 'text'):
            if key in value:
                value[key] = clean_text(value[key])
        for ref in dict.fromkeys(annotation_refs):
            cites = value.setdefault('citations', [])
            if any(cite.get('ref') == ref for cite in cites if isinstance(cite, dict)):
                continue
            role = 'target' if ref in source['plan_blocks'] else 'current'
            if ref in source['plan_blocks'] or records[ref]['role'] in {'actual_or_body', 'summary_and_next'}:
                cites.append({'ref': ref, 'role': role})
        for cite in value.get('citations', []):
            if isinstance(cite, dict) and 'quote' not in cite:
                cite['quote'] = source['plan_blocks'].get(cite.get('ref'), records.get(cite.get('ref'), {}).get('text', ''))
    return result


def audits_by_id(audit):
    checks = {}
    for row in audit.get('checks', []):
        if isinstance(row, dict):
            checks.setdefault(row.get('check_id'), []).append(row)
    return checks


def citations_for(raw, source, records, errors):
    cites = []
    for value in raw.get('citations', []):
        cite, error = p.checked_display_citation(value, source, records)
        if error:
            errors.append(error)
        else:
            cites.append(cite)
    return cites


def check_audit(value, checks, errors):
    found = checks.get(value.get('check_id'), [])
    if len(found) != 1 or found[0].get('supported') is not True:
        errors.append('assurance_semantic_audit_failed')


def forbidden_claim(text):
    return bool(re.search(r'通过验收|验收通过|认定违约|涉嫌造假|真实性存疑|(?:项目|综合|审查).{0,5}(?:评分|得分)|全部(?:任务|指标)?(?:均已)?(?:完成|达标)', text))


def validate_acceptance(raw, audit, context, source, records, *, include_summary=True):
    required = {item['item_id'] for item in context['plan_items']}
    rows = {row['item_id']: row for row in context['checked_alignments']}
    allowed_refs = {value['ref'] for value in context['plan_sources'] + context['report_sources']}
    checks = audits_by_id(audit)
    accepted, rejected, used = [], [], Counter()
    for value in raw.get('items', []):
        if not isinstance(value, dict):
            rejected.append({'errors': ['invalid_acceptance_shape']})
            continue
        item = copy.deepcopy(value)
        errors = []
        key = item.get('item_id')
        if key not in required or used[key]:
            errors.append('unknown_or_duplicate_acceptance_item')
        if item.get('basis_status') not in BASIS_STATUSES or item.get('scope_binding') not in SCOPES:
            errors.append('invalid_acceptance_status')
        if item.get('scope_binding') == '适用范围待确认' and item.get('basis_status') != '口径待确认':
            errors.append('unbound_acceptance_rule_used_as_conclusion')
        if any(not isinstance(item.get(field), dict) for field in ('expected', 'reported')):
            errors.append('missing_expected_or_reported_basis')
        if not isinstance(item.get('assessment'), str) or not item.get('assessment'):
            errors.append('empty_acceptance_assessment')
        if item.get('basis_status') != '依据对应' and (not item.get('question') or not item.get('topic_title')):
            errors.append('missing_acceptance_question')
        cites = citations_for(item, source, records, errors)
        if any(cite['ref'] not in allowed_refs for cite in cites):
            errors.append('acceptance_citation_outside_context')
        planned = ' '.join(cite['quote'] for cite in cites if cite['side'] == 'plan')
        actual = ' '.join(cite['quote'] for cite in cites if cite['side'] == 'report' and cite['role'] == 'current')
        if not planned or not actual:
            errors.append('acceptance_requires_plan_and_current_fact')
        expected_text = ' '.join(str(v) for v in item['expected'].values()) if isinstance(item.get('expected'), dict) else ''
        reported_text = ' '.join(str(v) for v in item['reported'].values()) if isinstance(item.get('reported'), dict) else ''
        other = ' '.join(str(item.get(field) or '') for field in ('assessment', 'question', 'topic_title'))
        if re.search(r'(?<![A-Za-z0-9_])C\d{3}(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])R-(?:PDF-P|T\d|P\d|S-)|(?<![A-Za-z0-9_])P-(?:CORE|T\d)', other):
            errors.append('internal_reference_in_acceptance_text')
        if not p.number_support(expected_text, planned) or not p.number_support(reported_text, actual) \
                or not p.number_support(other, planned + ' ' + actual):
            errors.append('unsupported_acceptance_number')
        if item.get('basis_status') == '存在口径出入' and re.search(r'未提及|未说明|未找到|没有说明|缺少说明', other):
            errors.append('absence_promoted_to_acceptance_mismatch')
        if re.search(r'未开展测试|没有测试|未进行测试|未做评测', other + reported_text) \
                and not re.search(r'未开展测试|没有测试|未进行测试|未做评测', actual):
            errors.append('absence_promoted_to_no_test')
        if c.asserts_delay(other) and not rows.get(key, {}).get('overdue'):
            errors.append('unsupported_acceptance_delay')
        claims = ' '.join((str(item.get('assessment') or ''), str(item.get('topic_title') or ''),
                           p.without_queried_completion(str(item.get('question') or ''))))
        if forbidden_claim(claims):
            errors.append('unsupported_acceptance_global_conclusion')
        check_audit(item, checks, errors)
        if errors:
            rejected.append({'item': value, 'errors': sorted(set(errors))})
        else:
            item['citations'] = cites
            accepted.append(item)
            used[key] += 1
    summary, summary_errors = (validate_summary(raw.get('summary'), audit, required, source, records)
                               if include_summary else (None, []))
    if summary_errors:
        rejected.append({'summary': raw.get('summary'), 'errors': summary_errors})
    return {'version': VERSION, 'items': accepted, 'summary': summary,
            'missing_item_ids': sorted(required - set(used))}, rejected


def validate_summary(raw, audit, required, source, records):
    summary, errors = copy.deepcopy(raw), []
    if not isinstance(summary, dict) or not isinstance(summary.get('text'), str) or not summary.get('text'):
        errors.append('missing_acceptance_summary')
        summary = {}
    cites = citations_for(summary, source, records, errors)
    if not summary.get('item_ids') or any(key not in required for key in summary.get('item_ids', [])):
        errors.append('invalid_acceptance_summary_items')
    if not cites or not p.number_support(str(summary.get('text', '')), ' '.join(cite['quote'] for cite in cites)):
        errors.append('unsupported_acceptance_summary')
    text = str(summary.get('text', ''))
    if re.search(r'(?<![A-Za-z0-9_])C\d{3}(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])R-(?:PDF-P|T\d|P\d|S-)|(?<![A-Za-z0-9_])P-(?:CORE|T\d)', text):
        errors.append('internal_reference_in_acceptance_text')
    if forbidden_claim(text):
        errors.append('unsupported_acceptance_global_conclusion')
    if re.search(r'(?:本期|中期).{0,15}[一二三四五六七八九十\d]+项.{0,18}(?:均已完成|均报告已完成|全部完成|都已完成)', text):
        errors.append('reviewed_subset_presented_as_all_current_tasks')
    check_audit(summary, audits_by_id(audit), errors)
    if errors:
        return None, sorted(set(errors))
    summary['citations'] = cites
    return summary, []


def retain_uncertain_acceptance(report, context, result):
    """Abstention is a process state, never an invented project deficiency."""
    result = copy.deepcopy(result)
    accepted = {item['item_id']: item for item in result['items']}
    rows = {row['item_id']: row for row in report['alignments']}
    unresolved = []
    for item in context['plan_items']:
        if item['item_id'] in accepted:
            accepted[item['item_id']]['review_state'] = 'approved'
            continue
        unresolved.append(item['item_id'])
        cites = [{'ref': item['source_ref'], 'quote': item['quote'], 'role': 'target', 'side': 'plan'}]
        cites += [{**cite, 'side': 'report'} for cite in rows[item['item_id']]['evidence'] if cite['role'] == 'current']
        accepted[item['item_id']] = {'item_id': item['item_id'], 'basis_status': '待复核',
            'scope_binding': '适用范围待确认', 'expected': {}, 'reported': {},
            'assessment': '认定依据待进一步核对。', 'topic_title': '', 'question': '',
            'citations': cites, 'review_state': 'needs_internal_review'}
    result['items'] = [accepted[item['item_id']] for item in context['plan_items']]
    result['internal_pending_item_ids'] = unresolved
    result['missing_item_ids'] = []
    if result.get('summary') and any(key in unresolved for key in result['summary'].get('item_ids', [])):
        result['summary'] = None
    result['summary_pending'] = bool(context['plan_items']) and not result.get('summary')
    return result


def followup_targets(report, source):
    targets = []
    for row in report['alignments']:
        if row['status'] not in {'部分完成', '尚未完成'} or not row.get('semantic_reviewed'):
            continue
        item = next(value for value in report['plan_items'] if value['item_id'] == row['item_id'])
        targets.append({'category': '本期继续推进的工作', 'item_ids': [item['item_id']],
                        'plan_ref': item['source_ref'], 'plan_quote': item['quote'], 'due': item['due'],
                        'known_progress': row['progress'], 'known_evidence': row['evidence']})
    for period in source['plan_periods']:
        if period['checkpoint_scope'] != '后续节点':
            continue
        # Use the whole future milestone, so old imperfect atomic extraction
        # cannot silently omit a future obligation from the follow-up list.
        linked = [item['item_id'] for item in report['plan_items'] if item['source_ref'] == period['id']]
        targets.append({'category': '后续阶段的任务', 'item_ids': linked, 'plan_ref': period['id'],
                        'plan_quote': source['plan_blocks'][period['id']], 'due': period['due']})
    for index, target in enumerate(targets, 1):
        target['work_id'] = f'W{index:02d}'
    return targets


def followup_context(report, source, records):
    targets = followup_targets(report, source)
    refs = {ref for ref, value in records.items() if value['role'] == 'summary_and_next'}
    for row in report['alignments']:
        if row['status'] == '后续节点' or row['status'] in {'部分完成', '尚未完成'}:
            refs.update(cite['ref'] for cite in row['evidence'] + row['next_steps'])
    for item in report['presentation'].get('follow_up_plans', []):
        refs.update(cite['ref'] for cite in item.get('citations', []) if cite['side'] == 'report')
    chosen = {ref: value for ref, value in records.items() if ref in refs}
    plan_refs = list(dict.fromkeys(target['plan_ref'] for target in targets))
    return {'project_name': report['project_profile']['project_name'], 'work_targets': targets,
            'plan_sources': [{'ref': ref, 'text': source['plan_blocks'][ref]} for ref in plan_refs],
            'report_sources': [{'ref': ref, **value} for ref, value in chosen.items()]}, chosen


def validate_followup(raw, audit, context, source, records):
    targets = {item['work_id']: item for item in context['work_targets']}
    checks = audits_by_id(audit)
    accepted, rejected, used = [], [], Counter()
    for value in raw.get('items', []):
        if not isinstance(value, dict):
            rejected.append({'errors': ['invalid_followup_shape']})
            continue
        item, errors = copy.deepcopy(value), []
        key = item.get('work_id')
        target = targets.get(key)
        if not target or used[key]:
            rejected.append({'item': value, 'errors': ['unknown_or_duplicate_followup_target']})
            continue
        cites = citations_for(item, source, records, errors)
        if not any(cite['ref'] == target['plan_ref'] and cite['side'] == 'plan' for cite in cites):
            errors.append('followup_missing_original_plan_source')
        if any(cite['side'] == 'plan' and cite['ref'] != target['plan_ref'] for cite in cites):
            errors.append('followup_other_task_as_requirement')
        if not isinstance(item.get('work'), str) or not item.get('work'):
            errors.append('empty_followup_work')
        arrangement = item.get('arrangement') or ''
        if arrangement and not any(cite['side'] == 'report' for cite in cites):
            errors.append('followup_arrangement_without_report_source')
        planned = ' '.join(cite['quote'] for cite in cites if cite['side'] == 'plan')
        reported = ' '.join(cite['quote'] for cite in cites if cite['side'] == 'report')
        if item.get('time_quote') and item.get('time_ref') in records:
            time_record = records[item['time_ref']]
            if c.literal_quote(item['time_quote'], time_record['text']):
                reported += ' ' + item['time_quote']
        if not p.number_support(str(item.get('work', '')), planned) or not p.number_support(arrangement, reported):
            errors.append('unsupported_followup_number')
        time_quote, time_ref = item.get('time_quote'), item.get('time_ref')
        item['report_time'] = ''
        if time_quote:
            record = records.get(time_ref, {})
            literal = c.literal_quote(time_quote, record.get('text', ''))
            if not literal or record.get('role') in {'target_restatement', 'proof_label', 'self_assessment'} \
                    or not re.search(r'\d{4}|\d{1,2}月|下一|后续|阶段|执行期|年底|年末|季度|月份|月底', str(time_quote)):
                errors.append('followup_time_not_from_report')
            else:
                item['report_time'] = literal
                cites.append({'ref': time_ref, 'quote': literal, 'role': 'future', 'side': 'report'})
        elif time_ref:
            errors.append('followup_time_ref_without_quote')
        if not arrangement and time_quote:
            errors.append('followup_time_without_arrangement')
        if c.asserts_delay(arrangement) or forbidden_claim(arrangement):
            errors.append('unsupported_followup_evaluation')
        check_audit(item, checks, errors)
        if errors:
            rejected.append({'item': value, 'errors': sorted(set(errors))})
        else:
            item.update(category=target['category'], item_ids=target['item_ids'], due=target['due'], citations=cites)
            accepted.append(item)
            used[key] += 1
    return {'version': VERSION, 'items': accepted, 'missing_work_ids': sorted(set(targets) - set(used))}, rejected


def retain_original_followup(context, result):
    """Keep original obligations even if generated report mappings abstain."""
    result = copy.deepcopy(result)
    approved = {item['work_id']: item for item in result['items']}
    unresolved = []
    for target in context['work_targets']:
        if target['work_id'] in approved:
            approved[target['work_id']]['review_state'] = 'approved'
            continue
        unresolved.append(target['work_id'])
        text = re.sub(r'^(?:[（(]\d+[）)]\s*)?\d{4}[^\n：:]{0,50}(?:[：:]|\n)\s*', '', target['plan_quote'], count=1)
        lines = [line.strip() for line in text.splitlines()
                 if line.strip().rstrip('：:') not in {'主要研究任务', '关键节点指标', '关键节点目标'}]
        work = ' '.join(lines).strip() or target['plan_quote']
        approved[target['work_id']] = {'work_id': target['work_id'], 'category': target['category'],
            'item_ids': target['item_ids'], 'work': work, 'arrangement': '', 'report_time': '',
            'time_ref': None, 'time_quote': '', 'due': target['due'],
            'citations': [{'ref': target['plan_ref'], 'quote': target['plan_quote'], 'role': 'target', 'side': 'plan'}],
            'review_state': 'report_mapping_pending'}
        approved[target['work_id']]['citations'] += [{**cite, 'side': 'report'} for cite in target.get('known_evidence', []) if cite['role'] == 'current']
    result['items'] = [approved[target['work_id']] for target in context['work_targets']]
    result['internal_pending_work_ids'] = unresolved
    result['missing_work_ids'] = []
    return result


def attach(report, acceptance, followup, rejections):
    report['presentation']['acceptance_basis'] = acceptance
    report['presentation']['followup_arrangements'] = followup
    if 'legacy_v0_4_next_step_plans' not in report:
        report['legacy_v0_4_next_step_plans'] = copy.deepcopy(report.get('next_step_plans', []))
    report['next_step_plans'] = followup['items']
    report['validation']['assurance_rejections'] = rejections
    if acceptance.get('internal_pending_item_ids') or followup.get('internal_pending_work_ids') or acceptance.get('summary_pending'):
        report['validation']['status'] = 'draft_with_internal_review'
    values = acceptance['items'] + followup['items'] + ([acceptance['summary']] if acceptance.get('summary') else [])
    lookup = {(entry['ref'], entry['quote']) for entry in report['source_index']}
    for value in values:
        for cite in value['citations']:
            key = (cite['ref'], cite['quote'])
            if key in lookup:
                continue
            lookup.add(key)
            report['source_index'].append({'source_id': f"依据{len(report['source_index']) + 1:02d}",
                'side': cite['side'], 'ref': cite['ref'], 'quote': cite['quote'], 'location': c.location(cite['ref']),
                'file_name': Path(report['review_scope']['plan_file' if cite['side'] == 'plan' else 'report_file']).name})
