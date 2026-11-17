"""Unify the stage summary and scope-bound opinion without duplicating questions."""
from __future__ import annotations

import copy
import re
from difflib import SequenceMatcher

import project_report_contract as c
import project_report_presentation as p
import project_report_assurance as a
import project_report_findings as f

VERSION = 'project-report-synthesis-v0.8.1'


def context_for(report, source, records):
    topics, notes = p.review_topics(report)
    cards = report.get('presentation', {}).get('evidence_notes', {}).get('items')
    if cards is None:
        cards, _ = f.evidence_context(report, source, records)
    sources = []
    for card in cards:
        quote = card.get('quote') or card.get('source_text', '')
        raw = source['plan_blocks'].get(card['ref'], '') if card['side'] == 'plan' else records.get(card['ref'], {}).get('text', '')
        if not c.literal_quote(quote, raw):
            raise ValueError('Conclusion source card does not match input')
        # A short display excerpt may omit a test condition or benchmark unit.
        # Give the writer and its independent auditor the same complete source
        # block, and bind citations to that literal block rather than borrowing
        # numbers from another source or silently accepting unsupported prose.
        sources.append({'source_id': card['source_id'], 'side': card['side'], 'ref': card['ref'],
                        'quote': raw, 'selected_excerpt': quote, 'use_for': card.get('use_for', ''),
                        'source_role': 'target' if card['side'] == 'plan' else records[card['ref']]['role']})
    allowed_ids = {key for topic in topics for key in topic['item_ids']} | {key for note in notes for key in note['item_ids']}
    rows = [row for row in report['alignments'] if p.visible_alignment(row) or row['item_id'] in allowed_ids]
    row_ids = {row['item_id'] for row in rows}
    card_ids = {(value['side'], value['ref']): value['source_id'] for value in sources}
    established = []
    for value in report['presentation'].get('acceptance_basis', {}).get('items', []):
        if value.get('basis_status') == '依据对应' and value.get('reported', {}).get('result'):
            established.append({key: copy.deepcopy(value.get(key)) for key in ('item_id', 'expected', 'reported', 'assessment')} |
                {'source_ids': list(dict.fromkeys(card_ids[(cite['side'], cite['ref'])] for cite in value.get('citations', []) if (cite['side'], cite['ref']) in card_ids))})
    legacy = report['presentation'].get('legacy_conclusion_inputs', {})
    return {'version': VERSION, 'project_name': report['project_profile']['project_name'],
        'editorial_policy': 'advisory' if report.get('run_metadata', {}).get('review_workflow') == 'grouped' else 'strict',
        'review_scope': {key: report['review_scope'].get(key) for key in ('review_stage', 'checkpoint_date', 'evidence_as_of')},
        'prior_text_for_editing_only': {
            'conclusion': [s['text'] for s in legacy.get('conclusions', [])] or [s['text'] for s in report['overall_assessment']['statements'] if s['section'] == 'conclusion'],
            'recognition_summary': (report['presentation'].get('acceptance_basis', {}).get('summary') or {}).get('text', '')},
        'established_reported_results': established,
        'plan_items': [item for item in report['plan_items'] if item['item_id'] in row_ids],
        'checked_alignments': [{key: row.get(key) for key in ('item_id', 'status', 'progress', 'reason', 'question', 'overdue', 'confirmation_kind', 'semantic_overrides')} for row in rows],
        'approved_basis': [{key: value.get(key) for key in ('item_id', 'basis_status', 'scope_binding', 'expected', 'reported', 'assessment', 'question')} for value in report['presentation'].get('acceptance_basis', {}).get('items', [])],
        'discussion_topics': [{key: value.get(key) for key in ('title', 'item_ids', 'category', 'certainty', 'difference', 'discuss_with_pi')} for value in report['presentation'].get('categorized_findings', {}).get('items', topics)],
        'pending_questions': [{key: note.get(key) for key in ('title', 'text', 'item_ids')} for note in notes],
        'allowed_issue_ids': sorted(allowed_ids), 'sources': sources}


def resolve(raw, context):
    draft = copy.deepcopy(raw)
    sources = {value['source_id']: value for value in context['sources']}
    as_of = context.get('review_scope', {}).get('evidence_as_of') or {}
    date_source = next((s for s in sources.values()
                        if s['ref'] == as_of.get('ref') and s['side'] == 'report'
                        and s['source_role'] in {'actual_or_body', 'summary_and_next'}
                        and c.literal_quote(as_of.get('quote'), s['quote'])), None)
    date_parts = re.fullmatch(r'(20\d{2})-(\d{2})(?:-(\d{2}))?', as_of.get('date') or '')
    date_pattern = None
    if date_source and date_parts:
        year, month, day = date_parts.groups()
        prefix = rf'截至\s*{year}\s*[年./-]\s*0?{int(month)}'
        if day and as_of.get('precision') == 'day':
            date_pattern = prefix + rf'\s*[月./-]\s*0?{int(day)}(?!\d)\s*日?'
        elif not day and as_of.get('precision') == 'month':
            date_pattern = prefix + r'(?:\s*月(?!\s*\d)|(?=[\s，,。；;]|$))'
    for index, paragraph in enumerate(draft.get('paragraphs', []), 1):
        paragraph['paragraph_id'] = f'CS{index:02d}'
        paragraph['kind'] = 'narrative'
        # Readable prose comes from one model-authored paragraph. Legacy
        # fragments must never be concatenated into the visible text.
        paragraph.pop('opinion', None)
        paragraph.pop('key_uncertainty', None)
        # Attach the literal, independently extracted progress date before audit.
        if (date_pattern and isinstance(paragraph.get('text'), str)
                and re.search(date_pattern, paragraph['text'])
                and isinstance(paragraph.get('source_ids'), list)
                and date_source['source_id'] not in paragraph['source_ids']):
            paragraph['source_ids'].append(date_source['source_id'])
        paragraph['citations'] = []
        for sid in paragraph.get('source_ids', []):
            if sid in sources:
                value = sources[sid]
                paragraph['citations'].append({key: value[key] for key in ('ref', 'quote', 'side')} |
                    {'role': 'current' if value['side'] == 'report' and value['source_role'] in {'actual_or_body', 'summary_and_next'} else 'target'})
    return draft


def citation_repair_context(draft, context):
    """Make each paragraph's frozen source-role contract explicit to the selector."""
    items = {item['item_id']: item for item in context['plan_items']}
    constraints = []
    for value in draft['paragraphs']:
        refs = {items[key]['source_ref'] for key in value['item_ids'] if key in items}
        # A whole-project assessment can legitimately have no item_ids. Do not
        # present an empty candidate list as if no plan evidence were available.
        # The model must still choose a relevant source; nothing is auto-cited.
        plan_options = [s for s in context['sources'] if s['side'] == 'plan' and (not refs or s['ref'] in refs)]
        constraints.append({'paragraph_id': value['paragraph_id'],
            'required_roles': ['plan', 'current_report'] if value['has_assessment'] else ['current_report'],
            'preserve_source_ids': value['source_ids'],
            'plan_candidate_scope': 'explicit_items' if refs else 'project_plan_requires_selection',
            'related_plan_source_ids': [s['source_id'] for s in plan_options],
            'plan_source_options': copy.deepcopy(plan_options) if value['has_assessment'] else []})
    return {'fixed_paragraphs': [{key: value[key] for key in
        ('paragraph_id', 'text', 'has_assessment', 'item_ids', 'issue_ids', 'source_ids')} for value in draft['paragraphs']],
        'citation_constraints': constraints, 'sources': context['sources'], 'plan_items': context['plan_items']}


def numeric_binding_feedback(draft, context, paragraph_ids):
    """Expose the exact numeric gap and retrieval leads to the repair writer."""
    output = []
    for paragraph in draft['paragraphs']:
        if paragraph['paragraph_id'] not in paragraph_ids:
            continue
        available = {s['source_id']: {**s, 'text': s['quote']} for s in context['sources']
                     if (s['side'] == 'report' and s['source_role'] in {'actual_or_body', 'summary_and_next'})
                     or paragraph['has_assessment'] and s['side'] == 'plan'}
        hints = p.numeric_source_hints(paragraph['text'], [{'ref': sid} for sid in paragraph['source_ids']], available)
        output.append({'paragraph_id': paragraph['paragraph_id'], 'missing_numbers': hints['missing_numbers'],
                       'candidate_source_ids': hints['candidate_refs']})
    return output


def rebind_citations(draft, bindings, context):
    """No prose/flag changes, dropped original sources, or invalid role repairs."""
    sources = {value['source_id']: value for value in context['sources']}
    changes = bindings.get('paragraphs')
    ids = [v['paragraph_id'] for v in draft['paragraphs']]
    if (not isinstance(changes, list) or len(changes) != len(ids) or
        any(not isinstance(v, dict) or set(v) != {'paragraph_id', 'source_ids'} for v in changes) or
        {v['paragraph_id'] for v in changes} != set(ids)):
        return None, ['citation_repair_paragraph_coverage_invalid']
    by_id = {v['paragraph_id']: v['source_ids'] for v in changes}; errors = []
    amended = copy.deepcopy(draft)
    for value in amended['paragraphs']:
        key = value['paragraph_id']; selected = by_id[key]
        if (not isinstance(selected, list) or not selected or
            any(not isinstance(sid, str) or sid not in sources for sid in selected) or len(selected) != len(set(selected))):
            errors.append('citation_repair_invalid_sources:' + key); continue
        if not set(value['source_ids']) <= set(selected):
            errors.append('citation_repair_dropped_original_sources:' + key)
        current = [sources[sid] for sid in selected if sources[sid]['side'] == 'report' and
                   sources[sid]['source_role'] in {'actual_or_body', 'summary_and_next'}]
        plan = [sid for sid in selected if sources[sid]['side'] == 'plan']
        if value['has_assessment'] and (not current or not plan):
            errors.append('citation_repair_missing_required_role:' + key)
        elif not value['has_assessment'] and (not current or len(current) != len(selected)):
            errors.append('citation_repair_progress_not_current_only:' + key)
        value['source_ids'] = selected
    return (None, errors) if errors else (resolve(amended, context), [])


def normalized(text):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', text).lower()


def repeated(left, right, limit=42):
    left, right = normalized(left), normalized(right)
    if not left or not right:
        return False
    match = SequenceMatcher(None, left, right, autojunk=False).find_longest_match()
    return match.size >= limit or (min(len(left), len(right)) >= 24 and
                                  SequenceMatcher(None, left, right, autojunk=False).ratio() > .72)


def patent_disclosure_used_as_filing_result(text, context):
    """Reject a known stage substitution even if the prose auditor misses it.

    Only an already approved acceptance state activates this guard. It neither
    resolves conflicting source claims nor establishes a project's filing state.
    """
    established = ' '.join(value.get('reported', {}).get('result', '')
        for value in context.get('established_reported_results', []))
    if not re.search(r'专利[^。；\n]{0,40}(?:已受理|获得受理|获受理)', established):
        return False
    if not re.search(r'专利[^。；\n]{0,24}交底书', text) or not re.search(r'(?:数量|成果)[^。；\n]{0,15}(?:满足|达到|达标)', text):
        return False
    # An accepted/submitted application can be summarized without repeating the
    # exact accepted wording. Finishing its disclosure is not such a summary.
    actual_filing = re.search(r'专利[^。；\n]{0,30}(?:已受理|获得受理|获受理)', text) or re.search(
        r'(?:已?完成|已?提交)[^。；\n]{0,12}(?:专利申请|专利的申请)(?!的?交底书)', text)
    return not actual_filing


def validate(draft, audit, context):
    errors, warnings = [], []
    paragraphs = draft.get('paragraphs', [])
    if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 2:
        errors.append('invalid_conclusion_paragraph_count')
        paragraphs = paragraphs if isinstance(paragraphs, list) else []
    checks = {}
    for check in audit.get('checks', []):
        checks.setdefault(check.get('paragraph_id'), []).append(check)
    module = audit.get('module_check', {})
    for field in ('reader_friendly', 'not_repetitive', 'scope_preserved', 'states_consistent'):
        if module.get(field) is not True:
            if context.get('editorial_policy') == 'advisory' and field in {'reader_friendly', 'not_repetitive'} and module.get(field) is False:
                warnings.append('conclusion_editorial_advice:' + field)
            else:
                errors.append('conclusion_module_audit_failed:' + field)
    state_checks = {}
    for check in audit.get('state_checks', []):
        state_checks.setdefault(check.get('item_id'), []).append(check)
    for result in context.get('established_reported_results', []):
        found = state_checks.get(result['item_id'], [])
        if len(found) != 1 or type(found[0].get('mentioned')) is not bool or found[0].get('consistent') is not True:
            errors.append('established_result_not_preserved:' + result['item_id'])
    allowed_items = {item['item_id'] for item in context['plan_items']}
    allowed_issues = set(context['allowed_issue_ids'])
    sources = {value['source_id']: value for value in context['sources']}
    discussed = {key for paragraph in paragraphs for key in paragraph.get('issue_ids', [])}
    pending_mentions = [note for note in context['pending_questions'] if discussed.intersection(note.get('item_ids', []))]
    # Detailed pending matters already have their own readable paragraphs. A
    # single key question can explain the conclusion; replaying several is the
    # checklist repetition this module is meant to remove. Grouped item IDs
    # count as one note, and categorized difference summaries remain allowed.
    if len(pending_mentions) > 1:
        if context.get('editorial_policy') == 'advisory':
            warnings.append('conclusion_repeats_multiple_pending_questions')
        else:
            errors.append('conclusion_repeats_multiple_pending_questions')
    for value in paragraphs:
        key, text = value.get('paragraph_id'), value.get('text')
        if not isinstance(text, str) or not text.strip() or '\n' in text or len(text) > 1000:
            errors.append('invalid_conclusion_text:' + str(key))
            continue
        if len(checks.get(key, [])) != 1 or checks[key][0].get('supported') is not True:
            errors.append('conclusion_paragraph_audit_failed:' + str(key))
        if not isinstance(value.get('source_ids'), list) or not value['source_ids'] or any(sid not in sources for sid in value['source_ids']):
            errors.append('conclusion_source_not_allowed:' + str(key))
        if not isinstance(value.get('item_ids'), list) or any(item not in allowed_items for item in value['item_ids']):
            errors.append('conclusion_item_not_allowed:' + str(key))
        if not isinstance(value.get('issue_ids'), list) or not set(value['issue_ids']) <= allowed_issues:
            errors.append('new_business_issue_in_conclusion:' + str(key))
        cites = value.get('citations', [])
        current = [cite for cite in cites if cite['role'] == 'current']
        if type(value.get('has_assessment')) is not bool:
            errors.append('missing_comparison_annotation:' + str(key))
        if value.get('has_assessment'):
            support = ' '.join(cite['quote'] for cite in cites)
            if not current or not any(cite['side'] == 'plan' for cite in cites):
                errors.append('assessment_requires_two_sided_sources')
        else:
            support = ' '.join(cite['quote'] for cite in current)
            if not current or any(cite['role'] != 'current' for cite in cites):
                errors.append('progress_not_current_report_only')
        if not p.number_support(text, support):
            errors.append('unsupported_conclusion_number:' + str(key))
        if patent_disclosure_used_as_filing_result(text, context):
            errors.append('patent_disclosure_cannot_support_filing_completion:' + str(key))
        # A full duplicate or a repeated preamble is an actual rendering error;
        # shared technical names and a brief key question are legitimate.
        if re.search(r'(.{8,32}?)\1', text):
            errors.append('repeated_phrase_in_conclusion')
        claims = p.asserted_completion_text(p.without_queried_completion(text))
        if a.forbidden_claim(claims) or re.search(
                r'本期[^，,。；;：:\n]{0,8}[一二三四五六七八九十\d]+项[^，,。；;：:\n]{0,14}(?:均已完成|均对应|全部完成)'
                r'|(?:所有|全部|各项)(?:本期|中期|阶段|计划|考核|项目)?(?:任务|指标|目标|工作|要求)'
                r'[^，,。；;：:\n]{0,8}(?:完成|达标)', claims):
            errors.append('unsupported_whole_project_conclusion')
        if re.search(r'核对条目|本期检查的|未拆解|校验降级|模型输出|C\d{3}|R-(?:PDF|T\d|P\d)|P-T\d', text):
            errors.append('internal_jargon_in_conclusion')
        if c.asserts_delay(text) and not any(row.get('overdue') and row['item_id'] in value.get('issue_ids', []) for row in context['checked_alignments']):
            errors.append('unsupported_conclusion_delay')
        for note in context['pending_questions']:
            if normalized(text) == normalized(note['text']):
                errors.append('conclusion_copies_pending_paragraph')
            elif repeated(text, note['text'], limit=48):
                warnings.append('shared_wording_with_pending:' + str(key))
    if len(paragraphs) == 2:
        left, right = (value.get('text', '') for value in paragraphs)
        if normalized(left) == normalized(right):
            errors.append('duplicate_conclusion_paragraphs')
        elif repeated(left, right):
            warnings.append('shared_wording_between_paragraphs')
    return {'version': VERSION, 'paragraphs': paragraphs, 'semantic_reviewed': not errors,
            'editorial_warnings': sorted(set(warnings)), 'audit': copy.deepcopy(audit)}, sorted(set(errors))


def attach(report, result, errors):
    pres = report['presentation']
    if 'legacy_conclusion_inputs' not in pres:
        pres['legacy_conclusion_inputs'] = {
            'conclusions': [copy.deepcopy(s) for s in report['overall_assessment']['statements'] if s['section'] == 'conclusion'],
            'recognition_summary': copy.deepcopy(pres.get('acceptance_basis', {}).get('summary'))}
    pres['integrated_conclusion'] = result
    replacement = [{**copy.deepcopy(value), 'section': 'conclusion'} for value in result['paragraphs']]
    for container in (report['overall_assessment'], pres):
        container['statements'] = [s for s in container.get('statements', []) if s['section'] != 'conclusion'] + copy.deepcopy(replacement)
    pres['missing_sections'] = sorted(p.SECTIONS - {value['section'] for value in pres['statements']})
    report['validation']['presentation_missing_sections'] = list(pres['missing_sections'])
    known = {(entry['ref'], entry['quote']) for entry in report['source_index']}
    for value in replacement:
        for cite in value['citations']:
            pair = (cite['ref'], cite['quote'])
            if pair not in known:
                known.add(pair)
                report['source_index'].append({'source_id': f"依据{len(report['source_index'])+1:02d}",
                    'ref': cite['ref'], 'quote': cite['quote'], 'side': cite['side'], 'location': c.location(cite['ref']),
                    'file_name': report['review_scope'][cite['side']+'_file'].split('/')[-1]})
    report['validation']['integrated_conclusion_rejections'] = errors


def reuse_evidence(report, source, records, previous):
    """The new conclusion was audited against these exact, already checked cards."""
    by_ref = {(card['side'], card['ref']): card for card in previous}
    groups, uses = f.evidence_context(report, source, records)
    result = []
    for group in groups:
        old = by_ref.get((group['side'], group['ref']))
        if not old or not old.get('semantic_reviewed') or not c.literal_quote(old['quote'], group['source_text']):
            raise ValueError('Conclusion refresh requires unchanged checked evidence cards')
        result.append({**copy.deepcopy(old), 'alias_ids': list(dict.fromkeys(group['alias_ids'] + [old['source_id']])),
                       'use_ids': [use['use_id'] for use in group['uses']]})
    report['presentation']['evidence_notes'] = {'version': f.VERSION, 'items': result, 'uses': uses,
        'reuse_reason': 'unchanged excerpts and purposes; new conclusion audited against these exact cards'}
