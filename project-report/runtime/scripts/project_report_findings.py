"""Categorized, source-bound findings and evidence explanations for reader reports."""
from __future__ import annotations

import copy
import re

import project_report_contract as c
import project_report_presentation as p
import project_report_assurance as a

VERSION = 'project-report-findings-v0.6.2'
CATEGORIES = ('进度与计划节点差异', '交付内容与技术规格差异', '数量与性能指标差异',
              '验收标准与测评口径差异', '报告表述不一致')
FIELDS = ('plan_requirement', 'reported_progress', 'difference', 'discuss_with_pi')
FIELD_LABELS = {'plan_requirement': '任务书要求', 'reported_progress': '报告完成情况',
                'difference': '出入情况', 'discuss_with_pi': '与 PI 对接'}


def context_for(report, source, records):
    topics, notes = p.review_topics(report)
    items = {item['item_id']: item for item in report['plan_items']}
    rows = {row['item_id']: row for row in report['alignments']}
    targets = []
    for index, topic in enumerate(topics, 1):
        linked = [rows[key] for key in topic['item_ids']]
        conflict = any(any(x.get('rule') == 'completion_and_progress_conflict'
                           for x in row.get('semantic_overrides', [])) for row in linked)
        definite = any(row['status'] in {'部分完成', '尚未完成'} for row in linked)
        basis_definite = any(x['basis_status'] == '存在口径出入' and x['item_id'] in topic['item_ids']
                             for x in report.get('presentation', {}).get('acceptance_basis', {}).get('items', []))
        plan_refs = {items[key]['source_ref'] for key in topic['item_ids']}
        report_refs = {cite['ref'] for cite in topic['citations'] if cite.get('side') == 'report'
                       or cite['ref'] in records}
        plan_items = [copy.deepcopy(items[key]) for key in topic['item_ids']]
        for item in plan_items:
            due = item.get('due', {})
            if due.get('date') and due.get('precision') == 'month':
                # The internal calendar date is a comparison boundary, not a
                # day specified by the taskbook. Preserve the source precision.
                due['date'] = due['date'][:7]
        targets.append({'topic_id': f'T{index:02d}', 'item_ids': topic['item_ids'],
            'approved_topic': topic, 'conflict': conflict,
            'must_remain_uncertain': conflict or not (definite or basis_definite),
            'overdue_supported': all(row.get('overdue') for row in linked),
            'plan_items': plan_items,
            'plan_sources': [{'ref': ref, 'text': source['plan_blocks'][ref]} for ref in source['plan_blocks'] if ref in plan_refs]
                + [{'ref': cite['ref'], 'text': source['plan_blocks'][cite['ref']]} for cite in topic['citations']
                   if cite['ref'] in source['plan_blocks'] and cite['ref'] not in plan_refs],
            'report_sources': [{'ref': ref, **record} for ref, record in records.items() if ref in report_refs]})
        selected = targets[-1]
        # Related evidence can include background experiments as well as the
        # actual conflicting statements. Require each recorded conflict pair,
        # rather than every related current-fact citation. Older snapshots
        # without pair provenance retain the conservative coverage check.
        conflicts = [value for row in linked for value in row.get('semantic_overrides', [])
                     if value.get('rule') == 'completion_and_progress_conflict']
        if conflicts and all(value.get('progress_ref') and value.get('counterevidence_ref')
                             for value in conflicts):
            selected['required_conflict_source_groups'] = [list(dict.fromkeys(
                [value['progress_ref'], value['counterevidence_ref']])) for value in conflicts]
        current_refs = {cite['ref'] for cite in topic['citations'] if cite.get('role') == 'current'}
        current_refs.update(cite['ref'] for row in linked for cite in row['evidence'] if cite.get('role') == 'current')
        selected['field_source_options'] = {
            'plan_requirement': [value['ref'] for value in selected['plan_sources']],
            'reported_progress': [value['ref'] for value in selected['report_sources'] if value['ref'] in current_refs],
            'difference': [value['ref'] for value in selected['plan_sources'] + selected['report_sources']],
            'discuss_with_pi': [value['ref'] for value in selected['plan_sources'] + selected['report_sources']]}
    return {'version': VERSION, 'project_name': report['project_profile']['project_name'],
            'review_scope': {key: report['review_scope'].get(key) for key in ('review_stage', 'checkpoint_date', 'evidence_as_of')},
            'categories': list(CATEGORIES), 'approved_topics': targets}, notes


def resolve(raw, source, records):
    result = copy.deepcopy(raw)
    for item in result.get('items', []):
        if not isinstance(item, dict):
            continue
        for field in FIELDS:
            value = item.get(field)
            if not isinstance(value, dict):
                continue
            for cite in value.get('citations', []):
                if isinstance(cite, dict) and 'quote' not in cite:
                    cite['quote'] = source['plan_blocks'].get(cite.get('ref'), records.get(cite.get('ref'), {}).get('text', ''))
    return result


def validate(raw, audit, context, source, records):
    targets = {item['topic_id']: item for item in context['approved_topics']}
    checks = {}
    for row in audit.get('checks', []):
        checks.setdefault(row.get('topic_id'), []).append(row)
    accepted, errors, used = [], [], set()
    for raw_item in raw.get('items', []):
        item = copy.deepcopy(raw_item)
        key = item.get('topic_id')
        target = targets.get(key)
        problems = []
        if not target or key in used:
            errors.append({'topic_id': key, 'errors': ['unknown_or_duplicate_topic']})
            continue
        used.add(key)
        if item.get('category') not in CATEGORIES:
            problems.append('invalid_category')
        if item.get('certainty') not in {'已明确出入', '待确认'}:
            problems.append('invalid_certainty')
        if target['must_remain_uncertain'] and item.get('certainty') != '待确认':
            problems.append('uncertainty_promoted_to_definite_difference')
        if target['conflict'] and item.get('category') != '报告表述不一致':
            problems.append('conflict_misclassified_as_performance_failure')
        if len(checks.get(key, [])) != 1 or checks[key][0].get('supported') is not True:
            problems.append('finding_semantic_audit_failed')
        allowed_plan = {value['ref'] for value in target['plan_sources']}
        allowed_report = {value['ref'] for value in target['report_sources']}
        all_cites = []
        if not isinstance(item.get('title'), str) or not item.get('title'):
            problems.append('missing_topic_title')
        for field in FIELDS:
            value = item.get(field)
            if not isinstance(value, dict) or not isinstance(value.get('text'), str) or not value.get('text'):
                problems.append('missing_finding_field:' + field)
                continue
            cites = []
            for raw_cite in value.get('citations', []):
                cite, error = p.checked_display_citation(raw_cite, source, records)
                if error:
                    problems.append(error)
                elif cite['ref'] not in allowed_plan | allowed_report:
                    problems.append('citation_outside_approved_topic')
                else:
                    cites.append(cite)
            if not cites:
                problems.append('finding_field_without_source:' + field)
            if field == 'plan_requirement' and any(cite['side'] != 'plan' for cite in cites):
                problems.append('requirement_not_from_taskbook')
            if field == 'reported_progress' and (not any(cite['role'] == 'current' for cite in cites)
                    or any(cite['side'] != 'report' or cite['role'] != 'current' for cite in cites)):
                problems.append('progress_not_from_current_report_fact')
            if field == 'difference' and not ({cite['side'] for cite in cites} >= {'plan', 'report'}):
                problems.append('difference_requires_two_sided_sources')
            if not p.number_support(value['text'], ' '.join(cite['quote'] for cite in cites)):
                problems.append('unsupported_finding_number:' + field)
            value['citations'] = cites
            all_cites.extend(cites)
        text = ' '.join([str(item.get('title', ''))] + [str(item.get(field, {}).get('text', '')) for field in FIELDS])
        if not p.number_support(str(item.get('title', '')), ' '.join(cite['quote'] for cite in all_cites)):
            problems.append('unsupported_finding_title_number')
        if c.asserts_delay(text) and not target['overdue_supported']:
            problems.append('unsupported_finding_delay')
        if a.forbidden_claim(text) or re.search(r'真实性|涉嫌造假|未拆解|校验降级|模型输出|C\d{3}|R-(?:PDF|T\d|P\d)|P-T\d', text):
            problems.append('unsupported_scope_or_internal_text')
        if target['conflict']:
            progress = item.get('reported_progress', {})
            needed_groups = target.get('required_conflict_source_groups') or [
                [cite['ref'] for cite in target['approved_topic']['citations'] if cite.get('role') == 'current']]
            selected_refs = {cite['ref'] for cite in progress.get('citations', [])}
            if any(not set(group) <= selected_refs for group in needed_groups):
                problems.append('conflict_source_side_omitted')
        if problems:
            errors.append({'topic_id': key, 'errors': sorted(set(problems))})
        else:
            item['item_ids'] = target['item_ids']
            item['citations'] = all_cites
            accepted.append(item)
    return {'version': VERSION, 'items': accepted,
            'missing_topic_ids': [key for key in targets if key not in {x['topic_id'] for x in accepted}]}, errors


def attach(report, result, errors):
    report['presentation']['categorized_findings'] = result
    report['validation']['categorized_findings_rejections'] = errors
    known = {(entry['ref'], entry['quote']) for entry in report['source_index']}
    for item in result['items']:
        for cite in item['citations']:
            pair = (cite['ref'], cite['quote'])
            if pair not in known:
                known.add(pair)
                report['source_index'].append({'source_id': f"依据{len(report['source_index']) + 1:02d}",
                    'side': cite['side'], 'ref': cite['ref'], 'quote': cite['quote'], 'location': c.location(cite['ref']),
                    'file_name': report['review_scope'][cite['side'] + '_file'].split('/')[-1]})


def evidence_context(report, source, records):
    """Build a usage graph; only evidence actually cited in the three chapters is exported."""
    lookup = {(entry['ref'], entry['quote']): entry['source_id'] for entry in report['source_index']}
    entries = {entry['source_id']: entry for entry in report['source_index']}
    uses = []
    def add(label, text, cites, origin=None):
        centralized = report.get('presentation', {}).get('pi_confirmation')
        if centralized is not None and origin:
            import project_report_followup as followup
            entry = followup.entry_for_origin(report, origin)
            if entry is None:
                raise ValueError('Missing centralized question for evidence usage')
            label, text = followup.label(entry), entry['text']
        ids = list(dict.fromkeys(lookup[(cite['ref'], cite['quote'])] for cite in cites))
        if ids:
            uses.append({'use_id': f'U{len(uses)+1:03d}', 'label': label, 'text': text, 'source_ids': ids})
            if centralized is not None and origin:
                uses[-1]['origin_key'] = origin
    for statement in report['overall_assessment']['statements']:
        add({'overview': '项目概况', 'plan_summary': '任务书计划摘要', 'achievements': '实际进展摘要', 'conclusion': '综合审查意见'}[statement['section']],
            statement['text'], statement.get('citations', []))
    bases = {item['item_id']: item for item in report['presentation'].get('acceptance_basis', {}).get('items', [])}
    by_id = {item['item_id']: item for item in report['plan_items']}
    for row in report['alignments']:
        if not p.visible_alignment(row):
            continue
        item = by_id[row['item_id']]
        cites = [{'ref': item['source_ref'], 'quote': item['quote']}] + row['evidence']
        basis = bases.get(row['item_id'])
        if basis:
            cites += basis['citations']
        add('计划与进展对照', item['quote'] + '；' + (basis['reported'].get('result') or row['progress']) + '；' + basis['assessment'] if basis
            else item['quote'] + '；' + row['progress'] + '；' + row['reason'], cites)
    for index, topic in enumerate(report['presentation']['categorized_findings']['items'], 1):
        for field in FIELDS:
            add('对接事项：' + topic['title'] + '／' + FIELD_LABELS[field], topic[field]['text'], topic[field]['citations'],
                'finding:' + topic.get('topic_id', str(index)) if field == 'discuss_with_pi' else None)
    _, notes = p.review_topics(report)
    for index, note in enumerate(notes, 1):
        cites = note.get('citations', []) or [{'ref': by_id[key]['source_ref'], 'quote': by_id[key]['quote']} for key in note['item_ids']]
        add('待确认事项：' + note.get('title', ''), note['text'], cites, 'pending:' + str(index))
    summary = report['presentation'].get('acceptance_basis', {}).get('summary')
    if summary and not report['presentation'].get('integrated_conclusion'):
        add('成果认定综述', summary['text'], summary['citations'])
    active = {sid for use in uses for sid in use['source_ids']}
    grouped = {}
    # A source block keeps its first assigned identity even when its original
    # short excerpt is no longer used after conclusion replacement. Choosing
    # the first *active excerpt* renumbered identical source blocks at export.
    canonical = {}
    for entry in report['source_index']:
        canonical.setdefault((entry['side'], entry['ref']), entry['source_id'])
    for card in report['presentation'].get('evidence_notes', {}).get('items', []):
        key = (card['side'], card['ref'])
        entry = entries.get(card['source_id'])
        if entry is None or (entry['side'], entry['ref']) != key:
            raise ValueError('Evidence identity does not match its source block')
        canonical[key] = card['source_id']
    for sid, entry in entries.items():
        if sid not in active:
            continue
        key = (entry['side'], entry['ref'])
        if key not in grouped:
            grouped[key] = {**copy.deepcopy(entry), 'alias_ids': [], 'uses': []}
            grouped[key]['source_id'] = canonical[key]
            grouped[key]['source_text'] = source['plan_blocks'][entry['ref']] if entry['side'] == 'plan' else records[entry['ref']]['text']
        grouped[key]['alias_ids'].append(sid)
    for group in grouped.values():
        group['uses'] = [use for use in uses if set(use['source_ids']) & set(group['alias_ids'])]
        group['source_segments'] = segments(group['source_text'])
    return list(grouped.values()), uses


def segments(text, width=420, sentence_boundaries=False):
    """Stable offsets partition the original string without rewriting any character."""
    output, start = [], 0
    while start < len(text):
        limit = min(start + width, len(text))
        breaks = list(re.finditer(r'[。；！？\n]', text[start:limit]))
        if breaks and sentence_boundaries:
            end = start + breaks[0].end()
        else:
            end = start + breaks[-1].end() if breaks and breaks[-1].end() >= 80 else limit
        output.append({'segment_id': len(output) + 1, 'start': start, 'end': end, 'text': text[start:end]})
        start = end
    return output


def resolve_evidence(raw, groups):
    result = copy.deepcopy(raw)
    by_id = {group['source_id']: group for group in groups}
    for value in result.get('items', []):
        group = by_id.get(value.get('source_id'))
        if group is None or ('start_segment' not in value and 'end_segment' not in value):
            continue
        first, last = value.get('start_segment'), value.get('end_segment')
        selected = group['source_segments']
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(selected):
            value['quote'] = ''
        else:
            value['quote'] = group['source_text'][selected[first-1]['start']:selected[last-1]['end']]
    return result


def evidence_batches(groups, limit=6, characters=22000):
    batch, size = [], 0
    for group in groups:
        amount = len(str(group))
        if batch and (len(batch) >= limit or size + amount > characters):
            yield batch
            batch, size = [], 0
        batch.append(group)
        size += amount
    if batch:
        yield batch


def validate_evidence(raw, audit, groups):
    required = {group['source_id']: group for group in groups}
    checks = {}
    for row in audit.get('checks', []):
        checks.setdefault(row.get('source_id'), []).append(row)
    accepted, errors, seen = [], [], set()
    for value in raw.get('items', []):
        key = value.get('source_id')
        group = required.get(key)
        problems = []
        if not group or key in seen:
            errors.append({'source_id': key, 'errors': ['unknown_or_duplicate_evidence']})
            continue
        seen.add(key)
        quote = c.literal_quote(value.get('quote', ''), group['source_text'])
        purpose = value.get('use_for', '')
        if not quote:
            problems.append('excerpt_not_contiguous_source_text')
        if not isinstance(purpose, str) or not purpose.strip():
            problems.append('missing_evidence_purpose')
        elif not p.number_support(purpose, quote or ''):
            problems.append('evidence_purpose_numbers_not_in_excerpt')
        if a.forbidden_claim(str(purpose)) or re.search(r'真实性|涉嫌造假|C\d{3}|R-(?:PDF|T\d|P\d)|P-T\d', str(purpose)):
            problems.append('unsupported_evidence_purpose_scope')
        if len(checks.get(key, [])) != 1 or checks[key][0].get('supported') is not True:
            problems.append('evidence_purpose_semantic_audit_failed')
        if problems:
            errors.append({'source_id': key, 'errors': problems})
        else:
            accepted.append({key: copy.deepcopy(group[key]) for key in ('source_id', 'alias_ids', 'side', 'ref', 'file_name', 'location')}
                            | {'quote': quote, 'use_for': purpose, 'use_ids': [use['use_id'] for use in group['uses']], 'semantic_reviewed': True})
    return accepted, errors


def anchor(project_id, source_id):
    return 'evidence-' + re.sub(r'[^a-z0-9-]', '-', str(project_id).lower()) + '-' + re.sub(r'\D', '', source_id)
