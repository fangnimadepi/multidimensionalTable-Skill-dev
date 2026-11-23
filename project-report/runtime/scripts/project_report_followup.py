"""Collect approved questions after the conclusion without changing findings."""
from __future__ import annotations

import copy
import re

import project_report_contract as c
import project_report_presentation as p
import project_report_findings as f
import project_report_assurance as a

VERSION = 'project-report-followup-v0.10.1'
PI_HEADING = '与PI老师确认事项'
MANAGER_LABEL = '项目经理人需确认的审查口径'


def targets_for(report):
    """Use existing scope metadata for routing; never infer project performance."""
    topics, notes = p.review_topics(report)
    categorized = report.get('presentation', {}).get('categorized_findings')
    selected = categorized['items'] if categorized is not None else topics
    if categorized is not None:
        selected = sorted(selected, key=lambda value: f.CATEGORIES.index(value['category']))
    scope_ids = {value['item_id'] for value in report.get('presentation', {}).get('acceptance_basis', {}).get('items', [])
                 if value.get('scope_binding') == '适用范围待确认' and value.get('basis_status') == '口径待确认'}
    # A technical fallback card uses scope_binding=适用范围待确认 too, but its
    # basis_status=待复核 is not an approved request to redefine acceptance scope.
    candidates = []
    for index, topic in enumerate(selected, 1):
        value = topic['discuss_with_pi']
        if isinstance(value, str):
            value = {'text': value, 'citations': topic['citations']}
        candidates.append({'origin_key': 'finding:' + topic.get('topic_id', str(index)),
            'title': topic['title'], 'item_ids': topic['item_ids'],
            'text': value['text'], 'citations': copy.deepcopy(value['citations']),
            'audience': 'manager' if topic['item_ids'] and set(topic['item_ids']) <= scope_ids else 'pi'})
    by_id = {value['item_id']: value for value in report['plan_items']}
    for index, note in enumerate(notes, 1):
        cites = note.get('citations', []) or [{'ref': by_id[key]['source_ref'], 'quote': by_id[key]['quote']} for key in note['item_ids']]
        candidates.append({'origin_key': 'pending:' + str(index), 'title': note.get('title') or '工作进展',
            'item_ids': note['item_ids'], 'text': note['text'], 'citations': copy.deepcopy(cites),
            'audience': 'manager' if note['item_ids'] and set(note['item_ids']) <= scope_ids else 'pi'})
    targets, seen = [], {}
    for value in candidates:
        signature = c.digest({'text': re.sub(r'\s+', '', value['text']), 'audience': value['audience'],
                              'citations': value['citations']})
        if signature in seen:
            target = seen[signature]
            target['origin_keys'].append(value['origin_key'])
            target['item_ids'] = list(dict.fromkeys(target['item_ids'] + value['item_ids']))
        else:
            target = {'entry_id': f'Q{len(targets)+1:02d}', 'title': value['title'], 'audience': value['audience'],
                'item_ids': copy.deepcopy(value['item_ids']), 'original_text': value['text'],
                'citations': value['citations'], 'origin_keys': [value['origin_key']]}
            targets.append(target); seen[signature] = target
    return targets


def context_for(report):
    targets = targets_for(report)
    return {'version': VERSION, 'project_name': report['project_profile']['project_name'],
            'approved_questions': [{key: copy.deepcopy(value[key]) for key in
                ('entry_id','title','audience','original_text')} for value in targets]}, targets


def resolve(raw, targets):
    draft = copy.deepcopy(raw)
    lookup = {target['entry_id']: target for target in targets}
    for value in draft.get('items', []):
        target = lookup.get(value.get('entry_id'))
        if target and target['audience'] == 'pi' and isinstance(value.get('text'), str):
            value['text'] = re.sub(r'请\s*PI(?=确认|说明|澄清|明确|介绍)', '请老师', value['text'])
    return draft


def validate(raw, audit, targets):
    expected = {value['entry_id']: value for value in targets}
    checks = {}
    for value in audit.get('checks', []):
        checks.setdefault(value.get('entry_id'), []).append(value)
    items, errors, seen = [], [], set()
    for value in raw.get('items', []):
        key = value.get('entry_id'); target = expected.get(key)
        if not target or key in seen:
            errors.append({'entry_id': key, 'errors': ['unknown_or_duplicate_question']}); continue
        seen.add(key); problems = []
        text = value.get('text')
        found = checks.get(key, [])
        check = found[0] if len(found) == 1 else {}
        for field in ('supported','questions_preserved','no_new_requests','audience_correct','no_new_claims'):
            if check.get(field) is not True:
                problems.append('question_audit_failed:' + field)
        if not isinstance(text, str) or not text.strip():
            problems.append('missing_question_text')
        else:
            if not p.number_support(text, target['original_text'] + ' ' + target['title']):
                problems.append('new_number_in_question')
            if a.forbidden_claim(p.without_queried_completion(text)) or re.search(r'[CU]\d{3}|R-(?:PDF|T\d|P\d)|P-T\d|依据\d+', text):
                problems.append('unsupported_question_scope_or_internal_reference')
            if c.asserts_delay(text) and not c.asserts_delay(target['original_text']):
                problems.append('new_delay_claim_in_question')
            if target['audience'] == 'manager' and not re.search(r'是否.{0,70}(?:纳入|适用|属于|本期|中期|考核|范围)|(?:本期|中期).{0,30}是否', text):
                problems.append('unbound_scope_question_lost_uncertainty')
        if problems:
            errors.append({'entry_id': key, 'errors': problems, 'reviewer_feedback': check})
        else:
            items.append({**copy.deepcopy(target), 'text': text, 'audit': copy.deepcopy(check),
                          'original_sha256': c.digest(target), 'semantic_reviewed': True})
    missing = [key for key in expected if key not in {value['entry_id'] for value in items}]
    return {'version': VERSION, 'items': items, 'missing_entry_ids': missing}, errors


def attach(report, result, errors):
    report['presentation']['pi_confirmation'] = result
    report['validation']['pi_confirmation_rejections'] = errors


def entry_for_origin(report, origin):
    return next((entry for entry in report['presentation']['pi_confirmation']['items']
                 if origin in entry['origin_keys']), None)


def label(entry):
    return (PI_HEADING if entry['audience'] == 'pi' else MANAGER_LABEL) + '：' + entry['title']


def reuse_evidence(report, source, records, previous):
    """Reuse literal evidence after an independently approved question paraphrase."""
    import project_report_evidence as evidence
    groups, uses = evidence.context_for(report, source, records)
    old_uses = {value['use_id']: value for value in previous['uses']}
    new_uses = {value['use_id']: value for value in uses}
    if set(old_uses) != set(new_uses):
        raise ValueError('Question relocation cannot add or remove evidence usage')
    transitions = []
    for key, value in new_uses.items():
        old = old_uses[key]
        if old['source_ids'] != value['source_ids']:
            raise ValueError('Question relocation changed cited source set')
        if old['text'] == value['text'] and old['label'] == value['label']:
            continue
        origin = value.get('origin_key')
        entry = entry_for_origin(report, origin)
        if not entry or not entry['semantic_reviewed'] or old['text'] != entry['original_text'] or value['text'] != entry['text']:
            raise ValueError('Question relocation lacks exact original and approved rewrite')
        transitions.append({'use_id': key, 'origin_key': origin, 'entry_id': entry['entry_id'],
            'original_label': old['label'], 'original_text': old['text'],
            'display_label': value['label'], 'display_text': value['text'],
            'source_ids': value['source_ids'], 'rewrite_audit': copy.deepcopy(entry['audit'])})
    cards = copy.deepcopy(previous['items'])
    required = {group['source_id']: group for group in groups}
    if set(required) != {card['source_id'] for card in cards}:
        raise ValueError('Question relocation changed active evidence cards')
    for card in cards:
        if set(card['use_ids']) != {value['use_id'] for value in required[card['source_id']]['uses']}:
            raise ValueError('Question relocation changed evidence contribution coverage')
    report['presentation']['evidence_notes'] = {**copy.deepcopy(previous), 'items': cards, 'uses': uses,
        'question_relocation': {'version': VERSION, 'original_uses_sha256': c.digest(previous['uses']),
            'original_uses': copy.deepcopy(previous['uses']), 'transitions': transitions,
            'basis': 'unchanged excerpts and source sets; question paraphrases independently checked against approved originals'}}


def validate_reuse(report):
    view = report['presentation']['pi_confirmation']
    targets = targets_for(report)
    checked, rejected = validate({'items': view['items']}, {'checks': [value['audit'] for value in view['items']]}, targets)
    errors = ['confirmation_rewrite:' + str(value) for value in rejected]
    if view.get('version') != VERSION or checked['missing_entry_ids']:
        errors.append('confirmation_view_incomplete')
    expected = {value['entry_id']: value for value in targets}
    for entry in view['items']:
        target = expected.get(entry['entry_id'])
        if not target or any(entry[key] != target[key] for key in ('origin_keys','item_ids','title','audience','original_text','citations')) or entry['original_sha256'] != c.digest(target):
            errors.append('confirmation_original_binding_changed')
    relocation = report['presentation']['evidence_notes'].get('question_relocation')
    if relocation:
        originals = relocation['original_uses']
        if c.digest(originals) != relocation['original_uses_sha256']:
            errors.append('confirmation_original_usage_hash_changed')
        old = {value['use_id']: value for value in originals}
        now = {value['use_id']: value for value in report['presentation']['evidence_notes']['uses']}
        links = {value['use_id']: value for value in relocation['transitions']}
        if set(old) != set(now):
            errors.append('confirmation_usage_coverage_changed')
        for key in old.keys() & now.keys():
            if old[key]['source_ids'] != now[key]['source_ids']:
                errors.append('confirmation_source_set_changed')
            if old[key]['text'] != now[key]['text'] or old[key]['label'] != now[key]['label']:
                transition = links.get(key, {})
                entry = entry_for_origin(report, now[key].get('origin_key'))
                if not entry or transition.get('entry_id') != entry['entry_id'] or old[key]['text'] != entry['original_text'] or now[key]['text'] != entry['text'] or transition.get('rewrite_audit') != entry['audit']:
                    errors.append('confirmation_usage_rewrite_not_audited')
    return errors
