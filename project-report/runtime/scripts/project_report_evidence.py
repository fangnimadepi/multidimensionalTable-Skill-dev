"""Context-preserving evidence cards and independently audited usage links."""
from __future__ import annotations

import copy
import re
from collections import Counter

import project_report_contract as c
import project_report_findings as f
import project_report_presentation as p
import project_report_assurance as a

VERSION = 'project-report-evidence-v0.9.0'
RELATIONS = ('直接说明', '共同支撑', '不同表述')


def canonicalize_references(text):
    """Expand citation shorthand only; its labels are not measured quantities."""
    def expand(match):
        start, end = int(match[1]), int(match[2])
        return '、'.join(f'依据{value:02d}' for value in range(start, end + 1)) if 0 <= end - start <= 50 else match[0]
    text = re.sub(r'依据\s*(\d{1,3})(?!\d)\s*(?:至|到|—|～|~|-)\s*(?:依据\s*)?(\d{1,3})(?!\d)', expand, text)
    return re.sub(r'依据\s*(\d{1,3}(?!\d)(?:\s*[、，,]\s*(?:依据\s*)?\d{1,3}(?!\d))*)',
                  lambda match: '、'.join('依据' + str(int(number)).zfill(2)
                      for number in re.findall(r'\d+', match[1])), text)


def readable_usage_references(text, uses):
    labels = {}
    for use in uses:
        label = use['label']
        if label.startswith('对接事项：'):
            label = '对接事项中的' + label.split('／')[-1] if '／' in label else '对接事项'
        else:
            label = label.split('：', 1)[0]
        labels[use['use_id']] = label
    return re.sub(r'U\d{3}(?!\d)', lambda match: labels.get(match[0], match[0]), text)


def segments(text):
    """Partition at sentence/line endings without dropping or rewriting text."""
    return [{'segment_id': i, 'start': match.start(), 'end': match.end(), 'text': match.group()}
            for i, match in enumerate(re.finditer(r'.+?(?:[。！？\n]|$)', text, re.S), 1)]


def context_for(report, source, records):
    groups, uses = f.evidence_context(report, source, records)
    canonical = {sid: group['source_id'] for group in groups
                 for sid in [group['source_id']] + group['alias_ids']}
    for use in uses:
        use['source_ids'] = list(dict.fromkeys(canonical[sid] for sid in use['source_ids']))
    by_ref = {(group['side'], group['ref']): group['source_id'] for group in groups}
    conflicts = []
    for topic in report['presentation']['categorized_findings']['items']:
        if topic['category'] == '报告表述不一致':
            ids = {by_ref[('report', cite['ref'])] for cite in topic['reported_progress']['citations']
                   if ('report', cite['ref']) in by_ref}
            if len(ids) > 1:
                conflicts.append(ids)
    for group in groups:
        sid, ref = group['source_id'], group['ref']
        group['source_segments'] = segments(group['source_text'])
        group['uses'] = [use for use in uses if sid in use['source_ids']]
        peers = {key for use in group['uses'] for key in use['source_ids'] if key != sid}
        conflict_peers = set().union(*(ids - {sid} for ids in conflicts if sid in ids)) if conflicts else set()
        group['conflict_source_ids'] = sorted(conflict_peers)
        group['allowed_related_source_ids'] = sorted(peers | conflict_peers)
        group['context_blocks'] = []
        if group['side'] == 'plan':
            period = next((v for v in source.get('plan_periods', []) if v['id'] == ref), {})
            group['column_context'] = '任务书计划要求' + ('；计划时段：' + period['heading'] if period.get('heading') else '')
        else:
            role = records[ref]['role']
            group['column_context'] = {'target_restatement': '总结报告中的计划要求转述', 'proof_label': '证明材料名称',
                'self_assessment': '项目组自评', 'summary_and_next': '总结及下一步计划（须按句义区分现状与安排）'}.get(role,
                    '目标完成情况栏' if ref.endswith('-ACTUAL') else '项目实施正文')
            base_ref = records[ref].get('base_ref', ref)
            for other_ref, record in records.items():
                if other_ref != ref and record.get('base_ref') == base_ref:
                    group['context_blocks'].append({'context_id': other_ref, 'ref': other_ref,
                        'label': c.location(other_ref), 'text': record['text'], 'role': record['role']})
            match = re.fullmatch(r'(R-(?:S-)?P(?:DF-P)?)(\d+)', ref)
            if match:
                for number in (int(match[2])-1, int(match[2])+1):
                    other_ref = match[1] + str(number)
                    if other_ref in records:
                        record = records[other_ref]
                        group['context_blocks'].append({'context_id': other_ref, 'ref': other_ref,
                            'label': c.location(other_ref), 'text': record['text'], 'role': record['role']})
        for block in group['context_blocks']:
            block['source_segments'] = segments(block['text'])
    return groups, uses


def writer_context(groups, all_groups, prior_cards):
    lookup = {group['source_id']: group for group in all_groups}
    prior = {card['source_id']: card for card in prior_cards}
    values = []
    for group in groups:
        value = copy.deepcopy(group)
        value.pop('source_text')
        for block in value['context_blocks']:
            block.pop('text')
        value['related_sources'] = [{'source_id': sid, 'side': lookup[sid]['side'],
            'location': lookup[sid]['location'], 'column_context': lookup[sid]['column_context'],
            'text': lookup[sid]['source_text']} for sid in group['allowed_related_source_ids']]
        if group['source_id'] in prior:
            value['prior_card_for_editing_only'] = prior[group['source_id']]
        values.append(value)
    return {'version': VERSION, 'evidence_sources': values}


def select(value, text, parts):
    first, last = value.get('start_segment'), value.get('end_segment')
    if type(first) is int and type(last) is int and 1 <= first <= last <= len(parts):
        return text[parts[first-1]['start']:parts[last-1]['end']]
    return ''


def unique_writer_items(raw, groups):
    """Keep unambiguous drafts only; ask the model to replace duplicates whole.

    Do not choose a duplicate arbitrarily or merge its prose/usage links. These
    are structural candidates, not semantically approved evidence cards.
    """
    expected = [group['source_id'] for group in groups]
    items = raw.get('items') if isinstance(raw, dict) else None
    if (not isinstance(items, list) or any(not isinstance(value, dict)
            or not isinstance(value.get('source_id'), str) or value['source_id'] not in expected for value in items)):
        return {'items': []}, [{'source_id': sid, 'reason': 'invalid_or_unknown_writer_card'} for sid in expected]
    counts = Counter(value['source_id'] for value in items)
    errors = [{'source_id': sid, 'reason': 'duplicate_writer_card' if counts[sid] > 1 else 'missing_writer_card'}
              for sid in expected if counts[sid] != 1]
    return {'items': [copy.deepcopy(value) for value in items if counts[value['source_id']] == 1]}, errors


def literal_card(group):
    """A source-view fallback with no newly generated business interpretation."""
    explanation = ('本段保留任务书该位置的完整约定，用于核对正文引用的计划要求；实际进展需结合相应报告材料。'
                   if group['side'] == 'plan' else
                   '本段保留总结报告该位置的完整原文，用于回查正文引用的进展、范围与成果状态；多来源对照需结合共同引用的材料。')
    return {'source_id': group['source_id'], 'start_segment': 1, 'end_segment': len(group['source_segments']),
            'explanation': explanation, 'related_source_ids': list(group['conflict_source_ids']),
            'context_excerpts': [{'context_id': block['context_id'], 'start_segment': 1,
                'end_segment': len(block['source_segments'])} for block in group['context_blocks'] if block['source_segments']],
            'usage_links': [{'use_id': use['use_id'],
                'relation': '不同表述' if set(use['source_ids']).intersection(group['conflict_source_ids']) else '共同支撑' if len(use['source_ids']) > 1 else '直接说明'}
                for use in group['uses']], 'display_mode': 'complete_literal_source'}


def literal_audit_batches(groups, drafts, limit=6, characters=38000, uses=36):
    """Bound the actual audit payload, including peers and every body use."""
    import json
    batch = []
    for group in groups:
        candidate = batch + [group]
        amount = len(json.dumps(audit_context(candidate, groups, drafts), ensure_ascii=False))
        if batch and (len(candidate) > limit or amount > characters
                      or sum(len(value['uses']) for value in candidate) > uses):
            yield batch
            batch = []
        batch.append(group)
    if batch:
        yield batch


def audit_coverage_targets(audit, groups):
    checks = {}
    for check in audit.get('checks', []):
        if isinstance(check, dict): checks.setdefault(check.get('source_id'), []).append(check)
    targets = []
    fields = ('excerpt_complete', 'explanation_supported', 'context_preserved', 'conflict_preserved')
    for group in groups:
        rows = checks.get(group['source_id'], [])
        uses = {}
        if len(rows) == 1:
            for use in rows[0].get('use_checks', []):
                if isinstance(use, dict): uses.setdefault(use.get('use_id'), []).append(use)
        wanted = [use for use in group['uses'] if len(uses.get(use['use_id'], [])) != 1
                  or not isinstance(uses[use['use_id']][0].get('supported'), bool)]
        if len(rows) != 1 or any(not isinstance(rows[0].get(field), bool) for field in fields):
            wanted = group['uses']
        if wanted:
            targets.append({**group, 'uses': wanted})
    return targets


def merge_audit_coverage(audit, supplement, groups):
    def index(value):
        result = {}
        for check in value.get('checks', []):
            if isinstance(check, dict): result.setdefault(check.get('source_id'), []).append(check)
        return result
    old, new = index(audit), index(supplement); result = {'checks': []}
    for group in groups:
        sid = group['source_id']; previous, supplied = old.get(sid, []), new.get(sid, [])
        if len(previous) != 1:
            negative = any(row.get(field) is False for row in previous
                for field in ('excerpt_complete','explanation_supported','context_preserved','conflict_preserved'))
            negative |= any(use.get('supported') is False for row in previous for use in row.get('use_checks', []))
            if len(supplied) == 1 and not negative:
                result['checks'].append(supplied[0])
            else: result['checks'].extend(previous)
            continue
        row = copy.deepcopy(previous[0]); patch = supplied[0] if len(supplied) == 1 else {}
        for field in ('excerpt_complete','explanation_supported','context_preserved','conflict_preserved'):
            if not isinstance(row.get(field), bool) and isinstance(patch.get(field), bool): row[field] = patch[field]
        uses = {}
        for use in row.get('use_checks', []): uses.setdefault(use.get('use_id'), []).append(use)
        additions = {}
        for use in patch.get('use_checks', []): additions.setdefault(use.get('use_id'), []).append(use)
        combined = []
        for use in group['uses']:
            key = use['use_id']; current = uses.get(key, [])
            if any(check.get('supported') is False for check in current):
                combined.append({'use_id':key,'supported':False,'reason':'保留原审核中明确的不支持判断。'})
            elif len(current) == 1 and isinstance(current[0].get('supported'), bool): combined += current
            elif len(additions.get(key, [])) == 1: combined.append(additions[key][0])
        row['use_checks'] = combined; result['checks'].append(row)
    return result


def resolve(raw, groups):
    draft = copy.deepcopy(raw)
    lookup = {group['source_id']: group for group in groups}
    for value in draft.get('items', []):
        group = lookup.get(value.get('source_id'))
        if not group:
            continue
        if isinstance(value.get('explanation'), str):
            # These are reference labels, not scientific quantities. Resolve
            # only the locations the writer itself selected, before auditing.
            value['explanation'] = canonicalize_references(value['explanation'])
            value['explanation'] = re.sub(r'任务[/／]指标\s*\d{1,3}(?!\d)', '相关任务', value['explanation'])
            related = value.get('related_source_ids')
            if isinstance(related, list):
                selected = set(re.findall(r'依据\d+', value['explanation']))
                additions = selected & set(group['allowed_related_source_ids']) - set(related)
                value['related_source_ids'] = related + sorted(additions)
        if group.get('require_full_source'):
            value['start_segment'], value['end_segment'] = 1, len(group['source_segments'])
        value['quote'] = select(value, group['source_text'], group['source_segments'])
        blocks = {block['context_id']: block for block in group['context_blocks']}
        for extra in value.get('context_excerpts', []):
            block = blocks.get(extra.get('context_id'))
            extra['quote'] = select(extra, block['text'], block['source_segments']) if block else ''
    return draft


def audit_context(groups, all_groups, drafts):
    lookup = {group['source_id']: group for group in all_groups}
    values = []
    for group in groups:
        peers = []
        for sid in group['allowed_related_source_ids']:
            peer = lookup[sid]
            peers.append({'source_id': sid, 'side': peer['side'], 'column_context': peer['column_context'],
                          'quote': drafts.get(sid, {}).get('quote', ''),
                          'full_source_text': peer['source_text']})
            if drafts.get(sid, {}).get('context_excerpts'):
                peers[-1]['context_excerpts'] = drafts[sid]['context_excerpts']
        value = {**group, 'related_sources': peers, 'selected_excerpt': drafts[group['source_id']]['quote']}
        # f.evidence_context also carries the original citation's short span.
        # It is NOT the newly selected excerpt and must not masquerade as one.
        value.pop('quote', None)
        values.append(value)
    return {'version': VERSION, 'evidence_sources': values,
            'evidence_draft': {'items': [drafts[group['source_id']] for group in groups]}}


def validate(raw, audit, groups, all_drafts):
    required = {group['source_id']: group for group in groups}
    checks = {}
    for check in audit.get('checks', []):
        checks.setdefault(check.get('source_id'), []).append(check)
    accepted, errors, seen = [], [], set()
    for value in raw.get('items', []):
        sid = value.get('source_id')
        group = required.get(sid)
        if not group or sid in seen:
            errors.append({'source_id': sid, 'errors': ['unknown_or_duplicate_evidence']})
            continue
        seen.add(sid)
        problems, diagnostics = [], {}
        quote = select(value, group['source_text'], group['source_segments'])
        if not quote or value.get('quote') != quote:
            problems.append('excerpt_not_exact_selected_source_range')
        related = value.get('related_source_ids')
        if not isinstance(related, list) or len(related) != len(set(related)) or not set(related) <= set(group['allowed_related_source_ids']):
            problems.append('invalid_related_evidence')
            related = []
        if group['conflict_source_ids'] and not set(related).intersection(group['conflict_source_ids']):
            problems.append('conflicting_related_source_omitted')
        extras, blocks, extra_seen = [], {v['context_id']: v for v in group['context_blocks']}, set()
        for extra in value.get('context_excerpts', []):
            key = extra.get('context_id'); block = blocks.get(key)
            text = select(extra, block['text'], block['source_segments']) if block else ''
            if not text or extra.get('quote') != text or key in extra_seen:
                problems.append('invalid_context_excerpt')
            else:
                extras.append({**copy.deepcopy(extra), 'ref': block['ref'], 'label': block['label'], 'role': block['role']})
            extra_seen.add(key)
        explanation = value.get('explanation')
        if isinstance(explanation, str):
            explanation = readable_usage_references(canonicalize_references(explanation), group['uses'])
        support = ' '.join([quote] + [v['quote'] for v in extras] + [all_drafts.get(key, {}).get('quote', '') for key in related]
                           + [extra.get('quote', '') for key in related for extra in all_drafts.get(key, {}).get('context_excerpts', [])])
        if not isinstance(explanation, str) or not explanation.strip():
            problems.append('missing_evidence_explanation')
        else:
            mentioned = set(re.findall(r'依据\d+', explanation))
            if not mentioned <= {sid, *related}:
                problems.append('undeclared_related_evidence_in_explanation')
            if not p.number_support(re.sub(r'依据\d+', '', explanation), support):
                problems.append('explanation_numbers_not_in_displayed_sources')
                diagnostics['unsupported_literals'] = sorted(p.presentation_numbers(re.sub(r'依据\d+', '', explanation))
                    - p.presentation_numbers(support, chinese_counts=True))
            if a.forbidden_claim(explanation) or re.search(r'完全可靠|绝对可靠|百分之百|真实性|涉嫌造假|[CU]\d{3}|R-(?:PDF|T\d|P\d)|P-T\d', explanation):
                problems.append('unsupported_explanation_scope')
            if c.asserts_delay(explanation) and not any(c.asserts_delay(use['text']) for use in group['uses']):
                problems.append('new_delay_claim_in_evidence_explanation')
        links = copy.deepcopy(value.get('usage_links', []))
        expected_uses = {use['use_id'] for use in group['uses']}
        found = checks.get(sid, [])
        check = found[0] if len(found) == 1 else {}
        use_checks = check.get('use_checks', [])
        reviewed_all = (len(use_checks) == len(expected_uses)
            and {v.get('use_id') for v in use_checks} == expected_uses
            and all(v.get('supported') is True for v in use_checks)
            and all(check.get(field) is True for field in ('excerpt_complete','explanation_supported','context_preserved','conflict_preserved')))
        completed_links = []
        # The graph already determines which body locations cite this source.
        # Complete clerical link metadata only AFTER every location has its own
        # positive semantic audit. This never manufactures an audit decision.
        if reviewed_all and len(links) == len({link.get('use_id') for link in links}) and {link.get('use_id') for link in links} <= expected_uses and all(link.get('relation') in RELATIONS for link in links):
            for use in group['uses']:
                if use['use_id'] not in {link['use_id'] for link in links}:
                    relation = '不同表述' if set(use['source_ids']).intersection(group['conflict_source_ids']) else '共同支撑' if len(use['source_ids']) > 1 else '直接说明'
                    links.append({'use_id': use['use_id'], 'relation': relation})
                    completed_links.append(use['use_id'])
        if len(links) != len(expected_uses) or {link.get('use_id') for link in links} != expected_uses or any(link.get('relation') not in RELATIONS for link in links):
            problems.append('usage_relation_coverage_mismatch')
            diagnostics['missing_writer_use_ids'] = sorted(expected_uses - {link.get('use_id') for link in links})
        for field in ('excerpt_complete', 'explanation_supported', 'context_preserved', 'conflict_preserved'):
            if check.get(field) is not True:
                problems.append('evidence_audit_failed:' + field)
        if len(use_checks) != len(expected_uses) or {v.get('use_id') for v in use_checks} != expected_uses or any(v.get('supported') is not True for v in use_checks):
            problems.append('evidence_usage_audit_failed')
            diagnostics['missing_audit_use_ids'] = sorted(expected_uses - {v.get('use_id') for v in use_checks})
        if problems:
            errors.append({'source_id': sid, 'errors': sorted(set(problems)), 'required_use_ids': sorted(expected_uses),
                           'reviewer_feedback': check, **diagnostics})
        else:
            accepted.append({key: copy.deepcopy(group[key]) for key in ('source_id','alias_ids','side','ref','file_name','location','column_context')}
                | {'quote': quote, 'start_segment': value['start_segment'], 'end_segment': value['end_segment'],
                   'explanation': explanation, 'related_source_ids': related, 'context_excerpts': extras,
                   'usage_links': links, 'use_ids': sorted(expected_uses),
                   'graph_completed_use_ids': sorted(set(completed_links + value.get('graph_completed_use_ids', []))),
                   'audit': copy.deepcopy(check), 'semantic_reviewed': True})
    for sid in required.keys() - seen:
        errors.append({'source_id': sid, 'errors': ['missing_evidence_card']})
    return accepted, errors


def link_explanation(text, report_id, known):
    return re.sub(r'依据\d+', lambda match: '['+match[0]+'](#'+f.anchor(report_id, match[0])+')'
                  if match[0] in known else match[0], canonicalize_references(text))
