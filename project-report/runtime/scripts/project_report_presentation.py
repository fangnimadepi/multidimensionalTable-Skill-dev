"""Reader-facing report editing, provenance checks and Markdown presentation."""
from __future__ import annotations

import copy
import re
from pathlib import Path

import project_report_contract as c

VERSION = 'project-report-presentation-v0.4.1'
SECTIONS = {'overview', 'plan_summary', 'achievements', 'conclusion'}
CORE_SECTIONS = SECTIONS - {'conclusion'}


def structural_item(item, source, items):
    """Recognize only labels, not unresolved substantive obligations."""
    quote = item['quote'].strip()
    plain = re.sub(r'^[（(]?\d+[）)、.]\s*', '', quote).rstrip('：: ')
    if plain in {'主要研究任务', '研究任务', '关键节点目标', '年度目标', '年度计划',
                 '考核指标', '成果形式', '性能指标', '知识产权指标'}:
        return True
    # A short noun heading followed by a separately retained requirement.
    # Restrict this to an unparsed standalone milestone line; never drop an
    # entire unparsed obligation just because it lacks a model judgement.
    if (item.get('extraction_method') != 'unparsed_source_fallback'
            or item.get('scope') != 'milestone' or len(quote) > 24
            or re.search(r'\d|[。；，：:]', quote)
            or re.match(r'完成|构建|建立|形成|开展|发布|实现|达到|提交|验证|研究|设计|开发|生成', quote)
            or not re.search(r'准备|验证|生成|构建|建设|研发|调研|开发|设计|测试|研究|集成$', quote)):
        return False
    lines = [line.strip() for line in source['plan_blocks'].get(item['source_ref'], '').splitlines() if line.strip()]
    if quote not in lines:
        return False
    pos = lines.index(quote)
    return pos + 1 < len(lines) and any(
        other['source_ref'] == item['source_ref'] and other['item_id'] != item['item_id']
        and other['quote'] in '\n'.join(lines[pos + 1:]) and len(other['quote']) > len(quote)
        for other in items)


def clean_inventory(items, source):
    kept, removed, seen = [], [], set()
    for item in items:
        key = (item['source_ref'], item['quote'])
        reason = 'structural_label' if structural_item(item, source, items) else 'duplicate_literal' if key in seen else None
        if reason:
            removed.append({'item_id': item['item_id'], 'source_ref': item['source_ref'],
                            'quote': item['quote'], 'reason': reason})
        else:
            kept.append(copy.deepcopy(item))
            seen.add(key)
    if not kept:
        raise ValueError('No substantive plan items after label filtering')
    return kept, removed


def visible_alignment(row):
    return (row.get('semantic_reviewed') is True and not row.get('validation_issues')
            and row['status'] not in {'后续节点', '总体目标参考'}
            and any(x['role'] == 'current' for x in row.get('evidence', []))
            and all((row.get('comparability') or {}).get(k) != 'no' for k in ('metric', 'object', 'unit'))
            and not re.search(r'暂未形成可直接采用|未能建立完整', row.get('progress', '')))


def label(item):
    text = item.get('requirement') or item['quote']
    text = re.sub(r'^\d{4}年[^：:]{0,30}[：:]\s*', '', text)
    text = re.sub(r'^[（(]?\d+[）)、.]\s*', '', text).strip('；;。 ')
    return text if len(text) <= 58 else text[:58] + '…'


def presentation_context(source, records, items, rows):
    current = [item for item in items if item['checkpoint_scope'] == '本轮节点']
    current_refs = list(dict.fromkeys(item['source_ref'] for item in current))
    return {'project_name': Path(source['plan_file']).stem,
            'review_stage': source['review_stage'], 'checkpoint_date': source['checkpoint_date'],
            'evidence_as_of': source['evidence_as_of'], 'plan_items': items,
            'plan_sources': [{'ref': ref, 'text': text} for ref, text in source['plan_blocks'].items()],
            'report_sources': [{'ref': ref, **record} for ref, record in records.items()],
            'current_plan_summary_context': {
                'allowed_item_ids': [item['item_id'] for item in current],
                'allowed_source_refs': current_refs,
                'plan_sources': [{'ref':ref,'text':source['plan_blocks'][ref]} for ref in current_refs if ref in source['plan_blocks']],
                'instruction': 'plan_summary只概括这里列出的本轮节点，item_ids和citations也限于这些编号。其他计划仅供理解项目背景，不纳入本期计划概述。'},
            'checked_alignments': rows,
            'allowed_conclusion_issue_ids': [row['item_id'] for row in rows if row.get('confirmation_kind') == 'business'
                or row['status'] in {'部分完成','尚未完成'} or row.get('attention')]}


def presentation_numbers(text, *, chinese_counts=False):
    # Normalized comparison supports equivalent typography (e.g. 76061 / 76,061)
    # without permitting a newly calculated percentage.
    def normalize(value):
        # These 20xx project materials use calendar shorthand such as 26年底.
        # Only an explicit calendar suffix qualifies; a duration (26年), a
        # percentage, an identifier or an explicitly written 1926 is unchanged.
        value = re.sub(r'(?<!\d)(\d{2})年(?=底|末|初|(?:0?[1-9]|1[0-2])月)', r'20\1年', value)
        # Dates written 2026.8.31 must not become the decimal token 2026.8.
        value = re.sub(r'((?:19|20)\d{2})[./-](0?[1-9]|1[0-2])[./-](0?[1-9]|[12]\d|3[01])(?!\d)',
                       r'\1年\2月\3日', value)
        value = re.sub(r'((?:19|20)\d{2})[./-](0?[1-9]|1[0-2])(?=[\s月:：—－\-至~～,，;；。()（）\[\]【】]|完成|节点|$)',
                       lambda match: match[1] + '年' + str(int(match[2])) + '月', value)
        # List labels are short ordinals. A long identifier followed by .X
        # (e.g. an application number after a colon) is source data, not a bullet.
        value = re.sub(r'(^|[：:；;。\s])\d{1,2}[）)、.](?=[\u4e00-\u9fffA-Za-z“「])', r'\1', value)
        return value
    return c.numeric_tokens(normalize(text), chinese_counts=chinese_counts)


def number_support(text, support):
    return presentation_numbers(text) <= presentation_numbers(support, chinese_counts=True)


def without_queried_completion(text):
    """Keep a completion question from counting as an asserted status."""
    qualifier = r'全部(?:任务|指标|目标|要求)?(?:均已)?'
    text = re.sub(r'((?:是否|能否)(?:已(?:经)?|均已)?)' + qualifier + r'(?=完成|达标)', r'\1', text)
    return re.sub(r'(是(?:已(?:经)?|均已)?)' + qualifier + r'(?=(?:完成|达标)[，,]?\s*还是)', r'\1', text)


def add_reader_source_bindings(statement, patch, records):
    """Source-only patch: never rewrite prose/scope or discard old evidence."""
    if not isinstance(patch, dict) or set(patch) != {'additional_refs'}:
        return None
    refs = patch['additional_refs']
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or ref not in records or records[ref]['role'] not in
                   {'actual_or_body', 'summary_and_next'} for ref in refs)
            or len(refs) != len(set(refs))):
        return None
    result = copy.deepcopy(statement)
    selected = {cite.get('ref') for cite in result.get('citations', [])}
    result['citations'] += [{'ref': ref, 'role': 'current'} for ref in refs if ref not in selected]
    return result if len(result['citations']) > len(statement['citations']) else None


def numeric_source_hints(text, citations, records, limit=8):
    """Retrieve likely locations; these suggestions never establish support."""
    selected = {cite.get('ref') for cite in citations if cite.get('ref') in records}
    sides = {records[ref].get('side') for ref in selected} - {None}
    support = ' '.join(records[ref]['text'] for ref in selected)
    missing = presentation_numbers(text) - presentation_numbers(support, chinese_counts=True)
    identifiers = set(re.findall(r'[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*', text))
    def lexical_terms(value):
        # Retrieval only: common small numerals otherwise rank every taskbook
        # paragraph before the relevant report by its P/R source identifier.
        value = re.sub(r'[^a-z\u4e00-\u9fff]', '', value.lower())
        return {value[index:index+2] for index in range(len(value)-1)}
    query = lexical_terms(text)
    scores = {}
    for ref, record in records.items():
        original = record.get('text', '')
        numbers = missing & presentation_numbers(original, chinese_counts=True)
        terms = {word for word in identifiers if word.lower() in original.lower()}
        if numbers:
            lexical = lexical_terms(original)
            overlap = len(query & lexical) / max(1, (len(query)*len(lexical)) ** .5)
            scores[ref] = (len(terms), len(numbers), int(record.get('side') in sides), overlap)
    ranked = sorted(scores, key=lambda ref: (tuple(-v for v in scores[ref]), ref))[:limit]
    return {'missing_numbers': sorted(missing), 'candidate_refs': ranked,
            'selected_refs': sorted(selected), 'retrieval_only': True}


def failed_current_comparison(items, rows):
    expected = {item['item_id'] for item in items if item['checkpoint_scope'] == '本轮节点'
                and item.get('extraction_method') == 'model_literal'}
    by_id = {row['item_id']: row for row in rows}
    technical = any(key not in by_id or by_id[key].get('validation_issues')
                    or by_id[key].get('semantic_reviewed') is not True for key in expected)
    return bool(expected and technical and not any(visible_alignment(by_id[key]) for key in expected if key in by_id))


def asserts_review_issue(text):
    # Absence of a review finding is not itself a new finding. Keep actual
    # missing-evidence claims and questions after a comma subject to binding.
    text = re.sub(r'(?:报告)?(?:未发现|没有发现|暂无|没有)[^，,。；;\n]{0,48}(?:差异(?:事项)?|出入(?:事项)?|待确认事项|需复核问题)', '', text)
    return re.search(r'报告未|未在报告|未提供|未直接提及|口径差异|(?<!无)(?<!不)需[^。；]*澄清|(?<!无)(?<!不)需[^。；]*确认|尚未完成|未达|逾期|延期', text)


def checked_display_citation(raw, source, records):
    if not isinstance(raw, dict):
        return None, 'invalid_display_citation'
    ref = raw.get('ref')
    if ref in source['plan_blocks']:
        quote = c.literal_quote(raw.get('quote', source['plan_blocks'][ref]), source['plan_blocks'][ref])
        if raw.get('role') != 'target' or not quote:
            return None, 'invalid_plan_display_citation'
        return {'ref': ref, 'quote': quote, 'role': 'target', 'side': 'plan'}, None
    # Selecting a known source ID is deterministic. If the model supplied an
    # explicit quote, still validate it strictly rather than guessing a match.
    quote = raw.get('quote', records.get(ref, {}).get('text', ''))
    citation, error = c.checked_citation({**raw, 'quote': quote}, records)
    return ({**citation, 'side': 'report'} if citation else None), error


def assign_display_ids(raw):
    result = copy.deepcopy(raw)
    for field, prefix in [('statements', 'S'), ('confirmation_notes', 'Q'), ('follow_up_plans', 'N')]:
        for index, value in enumerate(result.get(field, []), 1):
            if isinstance(value, dict):
                value['display_id'] = f'{prefix}{index:02d}'
    return result


def resolve_reader_sources(raw, source, records):
    """Materialize source selections before the independent semantic audit."""
    result = copy.deepcopy(raw)
    for field in ('statements', 'confirmation_notes'):
        for value in result.get(field, []):
            if not isinstance(value, dict):
                continue
            if isinstance(value.get('text'), str):
                # Internal condition identifiers stay in item_ids, not prose;
                # clean the display before its independent semantic audit.
                value['text'] = re.sub(r'[（(]C\d{3}(?:[、,，\s]+C\d{3})*[）)]', '', value['text'])
            for citation in value.get('citations', []):
                if not isinstance(citation, dict) or 'quote' in citation:
                    continue
                ref = citation.get('ref')
                citation['quote'] = source['plan_blocks'].get(ref, records.get(ref, {}).get('text', ''))
            # A verified factual as-of date may be used in a summary without
            # being repeated in its selected results paragraphs. Attach that
            # exact date quote, never the default checkpoint or cover proxy.
            if value.get('section') == 'achievements':
                date = source.get('evidence_as_of', {})
                ref, quote = date.get('ref'), date.get('quote')
                parts = str(date.get('date') or '').split('-')
                if (date.get('precision') in {'day', 'month'} and len(parts) == (3 if date.get('precision') == 'day' else 2)
                        and ref in records and c.literal_quote(quote, records[ref]['text'])):
                    year, month = map(int, parts[:2])
                    pattern = (rf'{year}\s*[年./-]\s*0?{month}\s*[月./-]\s*0?{int(parts[2])}(?:日)?'
                               if date['precision'] == 'day' else
                               rf'{year}\s*[年./-]\s*0?{month}(?:月(?!\s*\d)|(?=[\s，,。；;]|$))')
                    if re.search(pattern, value.get('text', '')):
                        citations = value.setdefault('citations', [])
                        if not any(x.get('ref') == ref for x in citations):
                            citations.append({'ref': ref, 'quote': quote, 'role': 'current'})
    return result


def validate_presentation(raw, audit, source, records, items, rows, required_sections=None):
    """No generated prose or plan time is published before both checks pass."""
    item_map = {x['item_id']: x for x in items}
    row_map = {x['item_id']: x for x in rows}
    checks = {}
    for check in audit.get('checks', []):
        if isinstance(check, dict):
            checks.setdefault(check.get('display_id'), []).append(check)
    output = {'version': VERSION, 'statements': [], 'confirmation_notes': [], 'follow_up_plans': []}
    rejected = []
    for field in ('statements', 'confirmation_notes', 'follow_up_plans'):
        for raw_value in raw.get(field, []):
            if not isinstance(raw_value, dict):
                rejected.append({'field': field, 'errors': ['invalid_display_shape']})
                continue
            value = copy.deepcopy(raw_value)
            errors = []
            linked = value.get('item_ids', [])
            if not isinstance(linked, list) or any(x not in item_map for x in linked):
                errors.append('invalid_display_item_ids')
                linked = []
            audits = checks.get(value.get('display_id'), [])
            if len(audits) != 1 or audits[0].get('supported') is not True:
                errors.append('display_semantic_audit_failed')
            if field == 'follow_up_plans':
                citation, error = c.checked_citation(value, records, future=True)
                if error:
                    errors.append(error)
                if (re.search(r'经费|财务|预算|决算|明细账|采购', str(value.get('quote', '')))
                        and not re.search(r'研发|模型|数据集|智能体|仿真平台', str(value.get('quote', '')))):
                    errors.append('financial_arrangement_outside_review_scope')
                value['citations'] = [{**citation, 'side': 'report'}] if citation else []
                value['time_text'] = '未明确'
                time_quote, time_ref = value.get('time_quote'), value.get('time_ref')
                if time_quote:
                    time_record = records.get(time_ref, {})
                    literal = c.literal_quote(time_quote, time_record.get('text', ''))
                    if (not literal or time_record.get('role') not in {'actual_or_body', 'summary_and_next'}
                            or not re.search(r'\d{4}|\d{1,2}月|下一|后续|阶段|执行期|年底|年末|季度|月份|月底', str(time_quote))):
                        errors.append('invalid_follow_up_time_source')
                    else:
                        value['time_text'] = literal
                        value['citations'].append({'ref': time_ref, 'quote': literal, 'role': 'future', 'side': 'report'})
                elif time_ref:
                    errors.append('time_ref_without_quote')
            else:
                text = value.get('text', '')
                if not isinstance(text, str) or not text.strip():
                    errors.append('empty_display_text')
                if field == 'statements' and value.get('section') not in SECTIONS:
                    errors.append('invalid_display_section')
                if field == 'confirmation_notes':
                    if not linked or any(row_map[x].get('confirmation_kind') != 'business'
                                         or row_map[x]['status'] != '需要确认' for x in linked):
                        errors.append('technical_issue_as_business_question')
                citations = []
                for raw_cite in value.get('citations', []):
                    cite, error = checked_display_citation(raw_cite, source, records)
                    if error:
                        errors.append(error)
                    else:
                        citations.append(cite)
                value['citations'] = citations
                section = value.get('section')
                # Actual progress needs actual report support; a planned number
                # is never sufficient just because the same item was linked.
                if section == 'achievements':
                    supporting = [x['quote'] for x in citations if x['role'] == 'current' and x['side'] == 'report']
                    if not supporting:
                        errors.append('achievement_without_current_source')
                elif section == 'plan_summary':
                    supporting = [x['quote'] for x in citations if x['side'] == 'plan']
                    supporting += [item_map[key]['quote'] + ' ' + str(item_map[key].get('period_heading', '')) for key in linked]
                    supporting += [source['checkpoint_date']]
                    plan_refs = {x['ref'] for x in citations if x['side'] == 'plan'}
                    plan_refs.update(item_map[key]['source_ref'] for key in linked)
                    # A merged table's year can be outside the selected monthly
                    # cell. Use its extracted parent-year context, not a guessed
                    # year or an invented exact day from a month-end conversion.
                    supporting += [str(period['year']) for period in source.get('plan_periods', [])
                                   if period['id'] in plan_refs and isinstance(period.get('year'), int)]
                    if any(item_map[key]['checkpoint_scope'] == '后续节点' for key in linked):
                        errors.append('future_node_in_current_plan_summary')
                else:
                    supporting = [x['quote'] for x in citations]
                    supporting += [item_map[key]['quote'] + ' ' + item_map[key].get('period_heading', '') for key in linked]
                    supporting += [row_map[key]['progress'] + ' ' + row_map[key]['reason'] for key in linked if not row_map[key].get('validation_issues')]
                    supporting += [x['quote'] for key in linked for x in row_map[key]['evidence']]
                if not citations and not linked:
                    errors.append('display_without_source')
                if not number_support(str(text), ' '.join(supporting)):
                    errors.append('unsupported_display_number')
                claims = without_queried_completion(str(text)) if field == 'confirmation_notes' else str(text)
                if re.search(r'通过验收|通过评审|(?:项目|履约|审查|综合|总体)(?:的)?(?:评分|得分)|整体风险(?:较高|较低|可控)|全部(?:任务|目标|要求)?(?:均已)?完成|所有.*(?:均已完成|均已满足)', claims):
                    errors.append('unsupported_global_rating')
                if re.search(r'校验降级|未拆解|字符覆盖|模型输出|模型引用|核对条目|技术复核|系统待复核', str(text)):
                    errors.append('internal_jargon_in_reader_text')
                if section == 'achievements' and re.search(r'预期|预计|有望|下一步|计划将|拟将', str(text)):
                    errors.append('future_expectation_in_achievements')
                if section == 'conclusion':
                    issue_words = asserts_review_issue(str(text))
                    if 'issue_ids' in value:
                        issues = value['issue_ids']
                        if (not isinstance(issues, list) or any(not isinstance(key,str) or key not in linked for key in issues)
                                or len(issues) != len(set(issues))):
                            errors.append('invalid_conclusion_issue_ids')
                        else:
                            allowed = {key for key,row in row_map.items() if row.get('confirmation_kind') == 'business'
                                or row['status'] in {'部分完成','尚未完成'} or row.get('attention')}
                            if not set(issues) <= allowed or any(row_map[key].get('confirmation_kind') == 'technical_validation' for key in issues):
                                errors.append('technical_failure_as_business_gap_in_conclusion')
                            if issue_words and not issues:
                                errors.append('conclusion_question_without_issue_binding')
                    elif issue_words and any(row_map[key].get('confirmation_kind') == 'technical_validation' for key in linked):
                        # Legacy responses have no issue attribution. Keep the
                        # conservative guard instead of guessing which item the question concerns.
                        errors.append('technical_failure_as_business_gap_in_conclusion')
            if errors:
                rejected.append({'field': field, 'value': raw_value, 'errors': sorted(set(errors)), 'audit': audits})
            else:
                output[field].append(value)
    required = SECTIONS if required_sections is None else set(required_sections)
    output['missing_sections'] = sorted(required - {x['section'] for x in output['statements']})
    covered = {key for note in output['confirmation_notes'] for key in note['item_ids']}
    output['uncovered_business_items'] = [row['item_id'] for row in rows
                                        if row.get('confirmation_kind') == 'business' and row['status'] == '需要确认'
                                        and row['item_id'] not in covered]
    return output, rejected


def attach_presentation(report, presentation, rejections):
    report['presentation'] = presentation
    report['overall_assessment']['statements'] = presentation['statements']
    report['validation']['presentation_rejections'] = rejections
    report['validation']['presentation_missing_sections'] = presentation['missing_sections']
    report['validation']['uncovered_business_items'] = presentation['uncovered_business_items']
    report['next_step_plans'] = presentation['follow_up_plans']
    lookup = {(entry['ref'], entry['quote']): entry['source_id'] for entry in report['source_index']}
    for field in ('statements', 'confirmation_notes', 'follow_up_plans'):
        for value in presentation[field]:
            for citation in value.get('citations', []):
                key = (citation['ref'], citation['quote'])
                if key not in lookup:
                    sid = f"依据{len(report['source_index']) + 1:02d}"
                    lookup[key] = sid
                    report['source_index'].append({'source_id': sid, 'side': citation['side'],
                        'file_name': Path(report['review_scope']['plan_file' if citation['side'] == 'plan' else 'report_file']).name,
                        'ref': citation['ref'], 'location': c.location(citation['ref']), 'quote': citation['quote']})
    report['presentation']['current_comparison_item_ids'] = [row['item_id'] for row in report['alignments'] if visible_alignment(row)]


def review_topics(report):
    """Group audited business questions without repeating PI topics below."""
    presentation = report.get('presentation', {})
    topics = copy.deepcopy(presentation.get('pi_coordination', {}).get('items', []))
    bases = presentation.get('acceptance_basis', {}).get('items', [])
    for basis in bases:
        if basis['basis_status'] != '存在口径出入':
            continue
        existing = next((item for item in topics if basis['item_id'] in item['item_ids']), None)
        if existing:
            existing['difference'] += ' ' + basis['assessment']
            existing['discuss_with_pi'] += ' ' + basis['question']
            existing['citations'] += basis['citations']
        else:
            topics.append({'title': basis['topic_title'], 'difference': basis['assessment'],
                           'discuss_with_pi': basis['question'], 'item_ids': [basis['item_id']],
                           'citations': basis['citations']})
    discussed = {key for item in topics for key in item['item_ids']}
    notes = [copy.deepcopy(note) for note in presentation.get('confirmation_notes', [])
             if not set(note['item_ids']) <= discussed]
    for basis in bases:
        if basis['basis_status'] != '口径待确认':
            continue
        if basis['item_id'] in discussed:
            row = next(row for row in report['alignments'] if row['item_id'] == basis['item_id'])
            if row.get('attention') and basis['scope_binding'] == '适用范围待确认':
                continue  # The existing topic already asks the phase question.
            existing = next(item for item in topics if basis['item_id'] in item['item_ids'])
            if basis['question'] not in existing['discuss_with_pi']:
                existing['discuss_with_pi'] += ' 对认定依据，还需明确：' + basis['question']
                existing['citations'] += basis['citations']
            continue
        existing = next((note for note in notes if basis['item_id'] in note['item_ids']), None)
        # The assessment is already shown in the comparison table. Here keep
        # only the actionable question instead of repeating that paragraph.
        text = basis['question']
        if existing:
            if text and text not in existing['text']:
                existing['text'] += ' 另请明确：' + text
            existing.setdefault('citations', []).extend(basis['citations'])
        else:
            notes.append({'title': basis['topic_title'], 'text': text, 'item_ids': [basis['item_id']],
                          'citations': basis['citations']})
    covered = discussed | {key for note in notes for key in note['item_ids']}
    items = {item['item_id']: item for item in report['plan_items']}
    for row in report['alignments']:
        if row.get('confirmation_kind') != 'business' or row['status'] != '需要确认' or row['item_id'] in covered:
            continue
        item = items[row['item_id']]
        notes.append({'title': label(item), 'item_ids': [row['item_id']],
                      'text': (row.get('question') or '请明确本项工作的对应情况。'),
                      'citations': [{'ref': item['source_ref'], 'quote': item['quote']}] + row['evidence']})
    return topics, notes


def render(report):
    from project_report_citations import enabled, render as centralized_render
    legacy = _render_legacy(report)
    return centralized_render(report, legacy) if enabled(report) else legacy


def _render_legacy(report):
    items, rows = report['plan_items'], report['alignments']
    by_id = {x['item_id']: x for x in items}
    by_row = {x['item_id']: x for x in rows}
    lookup = {(x['ref'], x['quote']): x['source_id'] for x in report['source_index']}
    presentation = report.get('presentation', {})
    centralized = presentation.get('pi_confirmation')
    basis_by_id = {item['item_id']: item for item in presentation.get('acceptance_basis', {}).get('items', [])}
    topics, pending_notes = review_topics(report)
    import project_report_findings as finding_view
    evidence_notes = presentation.get('evidence_notes', {}).get('items')
    aliases = {alias: entry['source_id'] for entry in (evidence_notes or []) for alias in entry['alias_ids']}

    def evidence_link(source_id):
        canonical = aliases.get(source_id, source_id)
        return f'[{canonical}](#{finding_view.anchor(report["project_id"], canonical)})'

    def source_refs(value):
        refs = [lookup[(x['ref'], x['quote'])] for x in value.get('citations', []) if (x['ref'], x['quote']) in lookup]
        if not refs:
            for key in value.get('item_ids', []):
                if key in by_id:
                    item = by_id[key]
                    refs.append(lookup[(item['source_ref'], item['quote'])])
        return '、'.join(dict.fromkeys(evidence_link(ref) for ref in refs))

    def row_refs(row):
        item = by_id[row['item_id']]
        citations = [{'ref': item['source_ref'], 'quote': item['quote']}] + row.get('evidence', [])
        return source_refs({'citations': citations})

    def paragraphs(section):
        result = []
        for statement in report['overall_assessment']['statements']:
            if statement['section'] == section:
                refs = source_refs(statement)
                result += [statement['text'] + (f'（{refs}）' if refs else ''), '']
        return result

    def due(item):
        value = item.get('due', {}).get('date')
        if not value:
            return ''
        return value[:7] if item['due'].get('precision') == 'month' else value

    def table_row(values, node_column):
        cells = []
        for index, value in enumerate(values):
            if index == node_column and (not value or str(value).strip() in {'未明确', '未单列时间', '未绑定具体本期节点'}):
                cells.append('')
            else:
                cells.append(c.cell(value))
        return '| ' + ' | '.join(cells) + ' |'

    scope = report['review_scope']
    lines = ['# 项目阶段履约审查报告', '', f"**项目：{report['project_profile']['project_name']}**", '',
             f"审查阶段：{scope['review_stage']}　｜　审查基准日：{scope['checkpoint_date']}（暂定）", '',
             ]
    import project_report_profile as profile
    lines += profile.header_lines(report.get('administrative_profile', {}))
    lines += ['## 一、项目概况与审查口径', '']
    lines += paragraphs('overview') or ['项目概况待补充，具体工作见下文计划与进展对照。', '']
    lines += [f"- 任务书：{profile.material_label(report, 'plan')}", f"- 总结报告：{profile.material_label(report, 'report')}",
              '- 文中“依据”编号可在本报告末尾查阅文件位置及原文。', '',
              '## 二、计划与实际进展总览', '', '**任务书计划**', '']
    lines += paragraphs('plan_summary') or ['本期计划详见下方任务书要求与节点。', '']
    lines += ['**实际进展**', '']
    lines += paragraphs('achievements') or ['实际进展按下表列示；整体工作摘要待补充。', '']
    lines += ['### 计划与进展对照', '']
    visible = [row for row in rows if visible_alignment(row)]
    if visible:
        lines += ['| 序号 | 任务书要求 | 计划节点 | 报告进展 | 完成情况及认定依据 | 来源 |', '| ---: | --- | --- | --- | --- | --- |']
        for index, row in enumerate(visible, 1):
            item = by_id[row['item_id']]
            basis = basis_by_id.get(row['item_id'])
            opinion = basis['assessment'] if basis else ('对应情况待确认，详见第三部分。' if row['status'] == '需要确认'
                       else row['status'] + '：' + row['reason'])
            refs = row_refs(row)
            if basis:
                refs = '、'.join(dict.fromkeys((refs + '、' + source_refs(basis)).split('、')))
            progress = basis.get('reported', {}).get('result') if basis else None
            lines += [table_row([index, item['quote'], due(item), progress or row['progress'], opinion, refs], 2)]
        lines.append('')
    else:
        lines += ['本期工作的对应情况需进一步明确，相关事项见第三部分。', '']
    lines += ['## 三、项目出入点与对接重点', '']
    if not topics:
        reference = '需明确的内容见综合审查意见下方的确认事项。' if centralized is not None else '需明确的内容见下方待确认事项。'
        lines += ['本节暂无单列的出入事项。' + (reference if pending_notes else ''), '']
    categorized = presentation.get('categorized_findings')
    if categorized is not None:
        categories = [category for category in finding_view.CATEGORIES if any(topic['category'] == category for topic in categorized['items'])]
        for group_index, category in enumerate(categories, 1):
            numeral = '一二三四五'[group_index-1]
            lines += [f'### （{numeral}）{category}', '']
            selected = [topic for topic in categorized['items'] if topic['category'] == category]
            for index, topic in enumerate(selected, 1):
                uncertainty = '（待确认）' if topic['certainty'] == '待确认' else ''
                lines += [f"#### {index}. {topic['title']}{uncertainty}", '']
                for field in finding_view.FIELDS:
                    if centralized is not None and field == 'discuss_with_pi':
                        continue
                    value = topic[field]
                    refs = source_refs(value)
                    lines += [f"**{finding_view.FIELD_LABELS[field]}：** {value['text']}" + (f'（{refs}）' if refs else ''), '']
    else:
        for index, topic in enumerate(topics, 1):
            refs = source_refs(topic)
            lines += [f"### {index}. {topic['title']}", '',
                      f"**出入情况：** {topic['difference']}" + (f'（{refs}）' if refs else ''), '']
            if centralized is None:
                lines += [f"**与 PI 对接：** {topic['discuss_with_pi']}", '']
    lines += ['### 综合审查意见', '']
    conclusion = paragraphs('conclusion')
    if conclusion:
        lines += conclusion
    else:
        lines += ['综合意见待补充。主要进展见第二部分。', '']
    recognition_summary = presentation.get('acceptance_basis', {}).get('summary')
    if recognition_summary and not presentation.get('integrated_conclusion'):
        refs = source_refs(recognition_summary)
        lines += [recognition_summary['text'] + (f'（{refs}）' if refs else ''), '']
    elif not presentation.get('integrated_conclusion') and presentation.get('acceptance_basis', {}).get('summary_pending'):
        lines += ['成果认定依据详见第二部分对照表。', '']
    if centralized is not None:
        import project_report_followup as followup
        pi_items = [item for item in centralized['items'] if item['audience'] == 'pi']
        manager_items = [item for item in centralized['items'] if item['audience'] == 'manager']
        if pi_items:
            lines += ['### ' + followup.PI_HEADING, '']
            for item in pi_items:
                refs = source_refs(item)
                lines += [item['text'] + (f'（{refs}）' if refs else ''), '']
        if manager_items:
            lines += ['**' + followup.MANAGER_LABEL + '**', '']
            for item in manager_items:
                refs = source_refs(item)
                lines += ['**' + item['title'] + '。** ' + item['text'] + (f'（{refs}）' if refs else ''), '']
    elif pending_notes:
        lines += ['### 待确认事项', '']
        for note in pending_notes:
            refs = source_refs(note)
            lines += [f"**{note.get('title') or '工作进展待确认'}。** {note['text']}" + (f'（{refs}）' if refs else ''), '']
    lines += ['## 附录：判断依据与原文摘录', '', '以下为任务书与总结报告原文。Word 位置按段落或表格标注，PDF 按文件页序标注。', '']
    if scope.get('ocr', {}).get('scanned'):
        lines += ['扫描材料引文与 OCR 文本核对，附录页序对应原 PDF。', '']
    for entry in (evidence_notes if evidence_notes is not None else report['source_index']):
        lines += [f'<a name="{finding_view.anchor(report["project_id"], entry["source_id"])}" id="{finding_view.anchor(report["project_id"], entry["source_id"])}"></a>', '', f"### {entry['source_id']}｜{'任务书' if entry['side'] == 'plan' else '总结报告'}", '',
                  f"文件：{entry['file_name']}  ", f"位置：{entry['location']}", '']
        if entry.get('column_context'):
            context_label = '原文栏目：' if re.search(r'-(?:ACTUAL|TARGET|SELF|PROOF)$', entry['ref']) else '原文语境：'
            lines += [context_label + entry['column_context'], '']
        lines += ['> ' + entry['quote'].replace('\n', '\n> '), '']
        if entry.get('explanation'):
            import project_report_evidence as evidence_view
            known = {card['source_id'] for card in evidence_notes}
            for extra in entry.get('context_excerpts', []):
                lines += ['**补充上下文｜' + extra['label'] + '**', '', '> ' + extra['quote'].replace('\n', '\n> '), '']
            lines += ['**引用说明：** ' + evidence_view.link_explanation(entry['explanation'], report['project_id'], known), '']
            if entry.get('related_source_ids'):
                lines += ['相关依据：' + '、'.join('['+sid+'](#'+finding_view.anchor(report['project_id'], sid)+')' for sid in entry['related_source_ids']), '']
            labels = []
            for use in presentation['evidence_notes']['uses']:
                if use['use_id'] in entry['use_ids']:
                    label = use['label']
                    if label == '计划与进展对照':
                        row = use['text'].split('；', 1)[0]
                        label += '（' + row[:48] + ('…' if len(row) > 48 else '') + '）'
                    if label not in labels:
                        labels.append(label)
            lines += ['对应正文：' + '；'.join(labels), '']
        elif entry.get('use_for'):
            lines += ['**引用用途：** ' + entry['use_for'], '']
    return '\n'.join(lines)
