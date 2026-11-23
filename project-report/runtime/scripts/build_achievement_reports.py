#!/usr/bin/env python3
"""Generate source-bound achievement/value reports from paired taskbooks/reports."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import achievement_prompts as prompts
import achievement_sources as sources
import build_contract_review as base
import project_report_contract as contract
import project_report_presentation as presentation
import project_report_profile as profile
from llm_runtime import parallel_map, call_scope
from llm_step_cache import restore_compatible_step, request_identity, REQUEST_PROTOCOL
from review_common import atomic_write_json

ROOT = Path(__file__).resolve().parent.parent
VERSION = 'achievement-report-v0.2.0'
CATEGORIES = {'问题背景', '已有方案', '代表成果', '核心增量', '结果与应用', '当前阶段', '后续计划', '支持条件'}
STATES = {'背景说明', '计划约定', '已形成', '已报送', '已批示', '已采纳', '已运行', '后续计划', '状态待明确'}
KINDS = {'事实归纳', '价值分析', '推进建议'}
QUESTIONS = {'problem': '解决什么问题', 'delivered': '已经做成什么', 'increment': '核心增量是什么',
             'results': '取得什么结果', 'stage': '当前到哪一步'}
RETAINED_FIELDS = ('value_title', 'executive_summary', 'fact_sheet', 'value_analysis')
STANCES = {'值得继续跟进', '有条件继续跟进', '暂不足以形成判断'}
JUDGEMENT_CHECKS = ('concrete_basis', 'reasoned_stance', 'bounded_followup', 'proportionate', 'compatible_with_retained')


def write_json(path, data):
    atomic_write_json(path, data)


def filename(name):
    return re.sub(r'[<>:"/\\|?*]', '_', name) + '.md'


def framework_path():
    private = ROOT / 'doc/成果系统梳理与表达工作框架.md'
    return private if private.exists() else ROOT / 'config/achievement_framework.md'


def framework():
    path = framework_path()
    text = path.read_text()
    # The method is context; project examples and their numerical facts are not.
    method = re.split(r'(?m)^#\s*三\s', text, maxsplit=1)[0]
    method = re.sub(r'!\[[^\]]*\]\([^\n]+\)', '', method)
    return {'file': str(path.relative_to(ROOT)), 'sha256': base.sha256(path), 'method_only': method}


def cached_call(run, pid, stage, system, payload, config, execute):
    tokens = (3600 if stage == 'forecast-write' else 14000 if stage == 'expression-write' else 8000 if stage.startswith('expression-repair')
              else 5000 if stage.startswith('expression-audit') else 2800 if stage == 'expression-paired'
              else 7600 if stage.startswith('facts-edit') else 5600 if stage.startswith('report-edit') else 2400)
    key=request_identity(system,payload,config,tokens)
    path = run / 'calls' / pid / f'request-{key[:16]}.json'
    def identical_legacy(item):
        usage=item['usage']
        if (item.get('system_prompt')!=system or json.dumps(item.get('request'),ensure_ascii=False)!=json.dumps(payload,ensure_ascii=False) or item.get('model')!=config['text_model']
                or usage.get('requested_max_tokens')!=tokens or usage.get('provider')!=config.get('provider','siliconflow')):return False
        return item['cache_key']==contract.digest({'version':item['version'],'prompts':item['prompt_version'],
            'system':system,'payload':payload,'model':config['text_model'],'api_base':config['api_base']})
    restore_compatible_step(path,key,run,ROOT,stage,identical_legacy)
    if path.exists():
        item = json.loads(path.read_text())
        if item.get('cache_key') == key:
            return item['answer'], {**item['usage'], 'cache_reused': True, 'cache_file': str(path.relative_to(ROOT))}
    if not execute:
        raise RuntimeError('No matching cached response: ' + stage)
    print(pid + ': request ' + stage, flush=True)
    answer, usage = base.call_json_model(system, payload, config, tokens)
    write_json(path, {'cache_key': key, 'cache_protocol': REQUEST_PROTOCOL, 'version': VERSION, 'prompt_version': prompts.VERSION,
        'model': config['text_model'], 'stage': stage, 'system_prompt': system,
        'request': payload, 'answer': answer, 'usage': usage})
    return answer, {**usage, 'cache_reused': False, 'cache_file': str(path.relative_to(ROOT))}


def resolve_facts(raw, records):
    facts, errors, seen = [], [], set()
    for index, item in enumerate(raw.get('facts', []), 1):
        value = copy.deepcopy(item); key = value.get('fact_id'); cites = []
        if value.get('category') == '当前到哪一步':
            value['category'] = '当前阶段'
        if not isinstance(key, str) or not re.fullmatch(r'F\d+', key) or key in seen:
            errors.append(f'fact_id_invalid:{index}'); continue
        seen.add(key)
        if value.get('category') not in CATEGORIES or value.get('state') not in STATES or not value.get('text') or not value.get('title'):
            errors.append('fact_fields_invalid:' + key); continue
        for citation in value.get('citations', []):
            ref = citation.get('ref'); record = records.get(ref, {})
            quote = contract.literal_quote(citation.get('quote'), record.get('text', ''))
            selection = 'literal_model_excerpt'
            if not quote and record.get('text') and ('quote' not in citation or
                    isinstance(citation.get('quote'), str) and citation['quote'].strip()):
                # A model is allowed to select a source location, never to
                # fabricate its excerpt. The independent audit below must
                # validate the fact using this exact full original block.
                quote = record['text']
                selection = 'full_original_block'
            if not quote:
                errors.append('fact_quote_missing:' + key); continue
            cites.append({'ref': ref, 'quote': quote, 'selection': selection})
        if not cites:
            errors.append('fact_no_source:' + key); continue
        sides = {records[c['ref']]['side'] for c in cites}
        # Align a category with an explicit taskbook-only planned state. Prose
        # and state stay unchanged; claimed completed outcomes never qualify.
        if (sides == {'plan'} and value['state'] in {'计划约定', '后续计划'}
                and value['category'] in {'代表成果', '核心增量', '结果与应用'}):
            value['category'] = '已有方案' if value['state'] == '计划约定' else '后续计划'
        if value['category'] in {'代表成果', '核心增量', '结果与应用'} and 'report' not in sides:
            errors.append('planned_as_actual:' + key)
        if value['category'] == '支持条件' and value['state'] == '计划约定' and 'plan' not in sides:
            errors.append('support_criteria_without_taskbook:' + key)
        support = '\n'.join(c['quote'] for c in cites)
        if not presentation.number_support(value['title'] + value['text'], support):
            errors.append('fact_number_unsupported:' + key)
        value['citations'] = cites; facts.append(value)
    required = {'问题背景', '代表成果', '核心增量', '结果与应用', '后续计划'}
    if not required <= {f['category'] for f in facts}:
        errors.append('missing_core_fact_categories')
    if not 8 <= len(facts) <= 24:
        errors.append('fact_count_out_of_range')
    return facts, errors


def audit_checks(audit, key, expected, fields):
    groups = {}
    for check in audit.get('checks', []):
        groups.setdefault(check.get(key), []).append(check)
    errors = []
    if set(groups) != set(expected):
        errors.append('audit_coverage_mismatch')
    for identifier in expected:
        entries = groups.get(identifier, [])
        if len(entries) != 1 or any(entries[0].get(field) is not True for field in fields):
            errors.append({'id': identifier, 'audit': entries})
    return errors


def repair_fact_citations(raw, errors, source, run, pid, config, execute, attempt, calls, feedback=None):
    repairable = ('fact_number_unsupported:', 'fact_quote_missing:', 'fact_no_source:')
    ids = {error.split(':', 1)[1] for error in errors if isinstance(error, str) and error.startswith(repairable)}
    if not ids:
        return raw
    targets = [f for f in raw.get('facts', []) if f.get('fact_id') in ids]
    hints, refs = [], set()
    for fact in targets:
        cited = {c.get('ref') for c in fact.get('citations', []) if c.get('ref') in source['records']}
        side = 'plan' if fact.get('category') == '支持条件' else None
        eligible = {ref: record for ref, record in source['records'].items() if side is None or record['side'] == side}
        hint = presentation.numeric_source_hints(fact['title'] + fact['text'], fact.get('citations', []), eligible)
        # A model can abbreviate P-T3-R1-S1 as the (nonexistent) parent
        # P-T3-R1. Numeric retrieval alone then yields no candidates. Offer
        # the real chunks of that exact parent, never create a synthetic
        # source or silently accept a different location.
        missing_refs = sorted({c.get('ref') for c in fact.get('citations', [])
                               if isinstance(c.get('ref'), str) and c['ref'] not in source['records']})
        split_refs = [ref for ref in eligible if any(
            re.fullmatch(re.escape(missing) + r'-S[1-9]\d*', ref) for missing in missing_refs)]
        literal_refs = []
        for citation in fact.get('citations', []):
            if citation.get('ref') in source['records'] or len(str(citation.get('quote', ''))) < 12:
                continue
            matches = [ref for ref, record in eligible.items()
                       if contract.literal_quote(citation.get('quote'), record['text'])]
            if len(matches) == 1:
                literal_refs.extend(matches)
        candidates = list(dict.fromkeys(split_refs + literal_refs + hint['candidate_refs']))
        refs.update(cited); refs.update(candidates)
        hints.append({'fact_id': fact['fact_id'], 'numbers_missing_from_selected_locations': hint['missing_numbers'],
                      'invalid_source_refs': missing_refs, 'split_source_options': split_refs,
                      'candidate_refs_for_retrieval_only': candidates,
                      'candidate_number_coverage': [{'ref': ref, 'side': source['records'][ref]['side'],
                          'matching_missing_literals': sorted(set(hint['missing_numbers']) &
                              presentation.presentation_numbers(source['records'][ref]['text'], chinese_counts=True))}
                          for ref in candidates]})
    payload = {'project_id': pid, 'facts_needing_citations': targets, 'citation_feedback': hints,
        'source_records': [{'ref': ref, **source['records'][ref]} for ref in sorted(refs)]}
    if feedback:
        payload['previous_selection_failure'] = feedback
    system = prompts.PRINCIPLES + '''
只修复给定事实的引用位置，不能修改事实文字、标题、类别和状态。
返回{"facts":[{"fact_id":"F01","citations":[{"ref":"准确原文编号"}]}]}。不要抄quote，程序回填完整来源后独立复核。
同一事实包含跨页的时间和数量时，应分别引用两个来源块；只在另一页出现的数字不能仍引用前一页。
逐条检查numbers_missing_from_selected_locations中的每个数字或日期：在你本次实际选择的citations中必须能找到，不能用source_records中未选中的段落补证。
例如固定事实带“截至某年某月某日”，原引用只有下一段的成果，必须另外选择给出该日期的段落，而不能只原样返回原引用。
candidate_number_coverage只说明哪些位置含相同字面数字，请结合对象和原文选择，不因数字相同引用无关经费或其他任务。
候选编号是程序检索提示，不自动证明同一指标。请根据整个事实的对象、条件和状态选择真正支持的原文。
invalid_source_refs中的编号不存在，不能原样返回。split_source_options是该原文位置实际切分后的编号，须保留-S1、-S2等后缀；根据原文选择一个或多个真实块，不要返回父编号。不存在真实支持时返回空citations，不编造来源。
不得使用省略号、拼接不同页的引文，不要返回其他字段。'''
    fixed, usage = cached_call(run, pid, f'facts-citations-{attempt}', system, payload, config, execute)
    calls.append({'stage': 'facts-citations', **usage})
    result = copy.deepcopy(raw); changes = {}
    for entry in fixed.get('facts', []):
        key = entry.get('fact_id')
        if key not in ids or key in changes:
            raise ValueError('Citation repair changed or duplicated a fact identifier')
        changes[key] = entry.get('citations', [])
    for fact in result.get('facts', []):
        if fact['fact_id'] in changes:
            repaired = changes[fact['fact_id']]
            seen = {citation.get('ref') for citation in repaired}
            # Cross-page repairs must not lose a previously selected date or
            # shared condition. Keep its full original block as context; the
            # independent audit checks the combined support again.
            for citation in fact.get('citations', []):
                ref = citation.get('ref')
                if ref in source['records'] and ref not in seen:
                    repaired.append({'ref': ref, 'quote': source['records'][ref]['text']}); seen.add(ref)
            fact['citations'] = repaired
    return result


def statements(draft):
    if 'long_sections' in draft:
        from achievement_expression import statements as expression_statements
        return expression_statements(draft)
    values = [draft.get('executive_summary')]
    values += [draft.get('fact_sheet', {}).get(key) for key in QUESTIONS]
    values += draft.get('value_analysis', []) + draft.get('continuation_analysis', [])
    values += [draft.get('briefing')]
    values += [draft.get('overall_judgement')]
    return [value for value in values if isinstance(value, dict)]


def resolve_report(raw, facts):
    draft = copy.deepcopy(raw); errors = []; facts_by_id = {f['fact_id']: f for f in facts}
    modern = 'overall_judgement' in draft
    for field in ('value_analysis', 'continuation_analysis'):
        if field not in draft:
            continue
        paragraphs = []
        for value in draft.get(field, []):
            for part in re.split(r'\n\s*\n', value.get('text', '')):
                if part.strip():
                    paragraphs.append({**value, 'text': part.strip()})
        draft[field] = paragraphs
    if set(draft.get('fact_sheet', {})) != set(QUESTIONS):
        errors.append('five_fact_questions_incomplete')
    if not draft.get('executive_summary') or not draft.get('value_title') or (not modern and not draft.get('briefing')):
        errors.append('report_required_sections_missing')
    if not 1 <= len(draft.get('value_analysis', [])) <= 4 or (not modern and not 1 <= len(draft.get('continuation_analysis', [])) <= 4):
        errors.append('report_analysis_sections_missing')
    if modern:
        if 'briefing' in draft or 'continuation_analysis' in draft:
            errors.append('legacy_sections_in_new_report')
        judgement = draft['overall_judgement']
        if not isinstance(judgement, dict):
            errors.append('judgement_missing'); draft['overall_judgement'] = {}; judgement = draft['overall_judgement']
        if judgement.get('stance') not in STANCES or judgement.get('kind') != '价值分析':
            errors.append('judgement_stance_or_kind_invalid')
        if not 60 <= len(judgement.get('text', '')) <= 300 or '\n' in judgement.get('text', ''):
            errors.append('judgement_must_be_one_bounded_paragraph')
        actual = [facts_by_id[key] for key in judgement.get('fact_ids', []) if key in facts_by_id]
        if not any(f['category'] in {'代表成果', '核心增量', '结果与应用'} and
                   f['state'] not in {'计划约定', '后续计划', '背景说明'} for f in actual):
            errors.append('judgement_without_actual_achievement')
    for index, statement in enumerate(statements(draft), 1):
        statement['statement_id'] = 'J01' if modern and statement is draft['overall_judgement'] else f'S{index:02d}'
        # The factual binding lives in fact_ids. Both single and grouped inline
        # reference decorations must disappear before quantity validation.
        statement['text'] = re.sub(r'[【\[（(]\s*F\d+(?:\s*[,，、;；]\s*F\d+)*\s*[】\]）)]', '', statement.get('text', ''))
        statement['text'] = re.sub(r'(^|[。；\n：:])\s*\d+[.)、]\s*(?=[\u4e00-\u9fffA-Za-z])', r'\1', statement['text'])
        ids = statement.get('fact_ids', [])
        if not ids or any(key not in facts_by_id for key in ids) or statement.get('kind') not in KINDS or not statement.get('text'):
            errors.append('statement_binding_invalid:' + statement['statement_id']); continue
        support = '\n'.join(c['quote'] for key in ids for c in facts_by_id[key]['citations'])
        if not presentation.number_support(statement['text'], support):
            errors.append('statement_number_unsupported:' + statement['statement_id'])
    support = '\n'.join(c['quote'] for f in facts for c in f['citations'])
    if not presentation.number_support(draft.get('value_title', ''), support):
        errors.append('title_number_unsupported')
    return draft, errors


def report_audit_errors(draft, audit):
    values = statements(draft)
    errors = audit_checks(audit, 'statement_id', [s['statement_id'] for s in values],
        ('supported', 'no_new_facts', 'stage_preserved', 'conflicts_preserved'))
    # A factual summary has no inference to assess. Preserve that raw audit
    # answer, while requiring inference support for analyses and suggestions.
    checks = {c.get('statement_id'): c for c in audit.get('checks', [])}
    for value in values:
        if value['kind'] in {'价值分析', '推进建议'} and checks.get(value['statement_id'], {}).get('reasonable_inference') is not True:
            errors.append('unreasonable_analysis:' + value['statement_id'])
    return errors


def judgement_audit_errors(draft, audit):
    errors = report_audit_errors({'overall_judgement': draft['overall_judgement']}, audit)
    checks = audit.get('judgement_checks', {})
    errors += ['judgement_' + key for key in JUDGEMENT_CHECKS if checks.get(key) is not True]
    return errors


def focus_bounded_judgement(draft, errors, facts, checked, run, pid, config, execute, attempt, calls):
    """Have the model repair one paragraph, then run all normal audits below."""
    if ('judgement_must_be_one_bounded_paragraph' not in errors
            or any(key not in draft for key in RETAINED_FIELDS)):
        return draft, errors
    retained = {key: copy.deepcopy(draft[key]) for key in RETAINED_FIELDS}
    raw, usage = cached_call(run, pid, f'report-judgement-focus-{attempt}', prompts.JUDGEMENT,
        {**checked, 'retained_narrative': retained_context(draft), 'editing_feedback': {
            'previous_judgement': draft['overall_judgement'],
            'actual_character_count': len(draft['overall_judgement'].get('text', '')),
            'max_characters': 300, 'recommended_characters': '120—220，包含汉字、字母、数字与标点',
            'instruction': '仅重写overall_judgement为一个自然段。保留事实、阶段及有依据的推进态度；选一两项代表成果说明理由，不再罗列全份成果和背景。引用只填fact_ids，不在text嵌入F编号。'}},
        config, execute)
    calls.append({'stage': 'report-judgement-focus', **usage})
    result, checked_errors = resolve_report({**retained, 'overall_judgement': raw.get('overall_judgement')}, facts)
    # Everything except that paragraph is preserved byte-for-byte as text and
    # fact bindings. Independent whole-report and judgment audits still follow.
    for key in RETAINED_FIELDS:
        if result.get(key) != retained[key]:
            raise ValueError('Focused judgment repair changed retained narrative')
    return result, checked_errors


def narrative_reuse_errors(report, root=ROOT):
    """Check that reused audit decisions belong to identical retained text/facts."""
    proof = report['validation'].get('narrative_reuse')
    if not proof:
        return []
    path = root / proof['report_file']
    if not path.is_file() or base.sha256(path) != proof['report_sha256']:
        return ['reused_report_changed']
    prior = json.loads(path.read_text()); errors = []
    if prior['source'] != report['source'] or prior['facts'] != report['facts']:
        errors.append('reused_facts_or_source_changed')
    if any(prior['draft'].get(key) != report['draft'].get(key) for key in RETAINED_FIELDS):
        errors.append('retained_narrative_changed')
    if prior['validation']['facts_audit'] != report['validation']['facts_audit']:
        errors.append('reused_fact_audit_changed')
    if report_audit_errors(prior['draft'], prior['validation']['report_audit']):
        errors.append('prior_report_audit_invalid')
    retained_ids = [s['statement_id'] for key in RETAINED_FIELDS if key != 'value_title'
                    for s in ([report['draft'][key]] if key == 'executive_summary' else
                              list(report['draft'][key].values()) if key == 'fact_sheet' else report['draft'][key])]
    prior_checks = {c['statement_id']: c for c in prior['validation']['report_audit']['checks']}
    current_checks = {c['statement_id']: c for c in report['validation']['report_audit']['checks']}
    if any(current_checks.get(key) != prior_checks.get(key) for key in retained_ids):
        errors.append('reused_statement_audit_changed')
    if report['validation']['report_audit'].get('title_supported') != prior['validation']['report_audit'].get('title_supported'):
        errors.append('reused_title_audit_changed')
    return errors


def render(report):
    from achievement_expression import is_expression, render as expression_render
    if is_expression(report):
        return expression_render(report)
    source, draft = report['source'], report['draft']
    facts = {f['fact_id']: f for f in report['facts']}
    used_fact_ids = {key for statement in statements(draft) for key in statement['fact_ids']}
    used_refs = list(dict.fromkeys(c['ref'] for s in statements(draft) for key in s['fact_ids'] for c in facts[key]['citations']))
    labels = {ref: f'成果依据{i:02d}' for i, ref in enumerate(used_refs, 1)}
    prefix = 'achievement-' + report['project_id'].lower() + '-'

    def text(statement):
        refs = list(dict.fromkeys(c['ref'] for key in statement['fact_ids'] for c in facts[key]['citations']))
        links = '、'.join(f'[{labels[ref]}](#{prefix}{i + 1:02d})' for i, ref in enumerate(used_refs) if ref in refs)
        return statement['text'] + ('（' + links + '）' if links else '')

    lines = ['# 项目阶段成果与后续推进分析报告', '', '**项目：' + source['project_name'] + '**', '',
        '报告用途：内部成果梳理与后续推进研判', '']
    lines += profile.header_lines(source['administrative_profile'])
    lines += ['**' + draft['value_title'] + '**', '', text(draft['executive_summary']), '',
        '- 任务书：' + Path(source['plan_file']).name,
        '- 总结报告：' + Path(source['report_file']).name,
        '- [配套项目阶段履约审查报告](' + report.get('related_review_href', '../项目阶段履约审查报告/' + filename(source['project_name'])) + ')', '',
        '## 一、成果事实提炼', '', '| 提炼维度 | 当前成果与进展 |', '| --- | --- |']
    for key, label in QUESTIONS.items():
        lines.append('| ' + label + ' | ' + contract.cell(text(draft['fact_sheet'][key])) + ' |')
    lines += ['', '## 二、领域背景与成果价值', '']
    for value in draft['value_analysis']:
        lines += [text(value), '']
    if 'overall_judgement' in draft:
        lines += ['### 综合判断', '', text(draft['overall_judgement']), '']
    else:  # Preserve reproducibility of approved v0.1 reports.
        lines += ['## 三、后续推进依据与建议', '']
        for value in draft['continuation_analysis']:
            lines += [text(value), '']
        lines += ['## 四、汇报表达参考', '', '### ' + draft['value_title'], '', text(draft['briefing']), '']
    lines += ['## 附录：成果判断依据', '']
    if source.get('scanned_report'):
        lines += ['总结报告为扫描PDF，以下报告摘录为本地OCR识别文本，位置按原文件页码标注。', '']
    for index, ref in enumerate(used_refs, 1):
        record = source['records'][ref]
        contributing = [f for f in report['facts'] if f['fact_id'] in used_fact_ids and any(c['ref'] == ref for c in f['citations'])]
        lines += [f'<a name="{prefix}{index:02d}" id="{prefix}{index:02d}"></a>', '',
            '### ' + labels[ref] + '｜' + ('任务书' if record['side'] == 'plan' else '总结报告'), '',
            '- 文件：' + Path(record['file']).name, '- 位置：' + record['location'], '- 原文栏目：' + record['section'], '',
            '**原文及同段语境：**', '']
        lines += ['> ' + line for line in record['text'].splitlines()]
        lines += ['', '**本段支撑的事实：** ' + '；'.join(f['text'] for f in contributing), '']
    return '\n'.join(lines)


def checked_context(report):
    source, facts = report['source'], report['facts']
    return {'project_id': report['project_id'], 'project_name': source['project_name'],
        'framework_method': report['run_metadata']['framework']['method_only'], 'checked_facts': facts,
        'fact_source_context': {c['ref']: source['records'][c['ref']] for f in facts for c in f['citations']}}


def retained_context(draft):
    # Context is not an extra set of review targets. Hide old statement IDs to
    # prevent the reviewer accidentally auditing them in place of J01.
    def strip_ids(value):
        if isinstance(value, dict):
            return {key: strip_ids(item) for key, item in value.items() if key != 'statement_id'}
        if isinstance(value, list):
            return [strip_ids(item) for item in value]
        return value
    return strip_ids({key: draft[key] for key in RETAINED_FIELDS})


def revise_judgement(report, run, config, execute, stage='judgement', feedback=None):
    """Only a model may write the new judgment; freeze all previously approved prose."""
    retained = {key: copy.deepcopy(report['draft'][key]) for key in RETAINED_FIELDS}
    checked = {**checked_context(report), 'retained_narrative': retained_context(report['draft'])}
    calls = []; feedback = feedback or []
    for attempt in range(3):
        raw, usage = cached_call(run, report['project_id'], f'{stage}-edit-{attempt}', prompts.JUDGEMENT,
            {**checked, 'editing_feedback': feedback}, config, execute)
        calls.append({'stage': stage + '-edit', **usage})
        draft, errors = resolve_report({**retained, 'overall_judgement': raw.get('overall_judgement')}, report['facts'])
        if any(draft.get(key) != value for key, value in retained.items()):
            raise ValueError('Judgment refresh changed retained narrative')
        audit, usage = cached_call(run, report['project_id'], f'{stage}-audit-{attempt}', prompts.JUDGEMENT_AUDIT,
            {**checked, 'value_title': draft['value_title'], 'draft_statements': [draft['overall_judgement']]}, config, execute)
        calls.append({'stage': stage + '-audit', **usage})
        errors += judgement_audit_errors(draft, audit)
        if not errors:
            report['draft'] = draft
            retained_ids = {s['statement_id'] for s in statements(draft)} - {'J01'}
            checks = [c for c in report['validation']['report_audit']['checks'] if c['statement_id'] in retained_ids]
            report['validation']['report_audit'] = {'checks': checks + audit['checks'],
                'title_supported': report['validation']['report_audit']['title_supported']}
            report['validation']['judgement_audit'] = audit
            report['run_metadata']['calls'] += calls
            return
        feedback = {'errors': errors, 'previous_judgement': draft['overall_judgement'], 'audit': audit}
    write_json(run / 'failed-judgement' / f"{report['project_id']}.json", {'feedback': feedback})
    raise ValueError('Achievement judgment did not pass independent checks')


def refresh_judgement(pair, run, output, previous_dir, review_markdown_dir, config, execute):
    import check_achievement_report_artifacts as checker
    previous_path = previous_dir / f'{pair.project_id}.json'
    previous = json.loads(previous_path.read_text())
    errors = checker.check(previous, render(previous), ROOT, require_paired=True)
    if errors or previous['run_metadata']['framework']['method_only'] != framework()['method_only']:
        raise ValueError('Previous report/source/audit is not reusable: ' + str(errors))
    if sources.extract(pair) != previous['source']:
        raise ValueError('Refresh pair differs from previously audited sources')
    review_md = review_markdown_dir / filename(previous['source']['project_name'])
    paired = previous['validation']['paired_report_consistency']
    review = json.loads((ROOT / paired['review_report_file']).read_text())
    if not review_md.is_file() or review_md.read_text() != contract.render_markdown(review):
        raise ValueError('Related review Markdown differs from checked review JSON')
    report = copy.deepcopy(previous); report['schema_version'] = VERSION
    # Prior full-report audits remain attached to unchanged statements only.
    report['validation'].pop('paired_report_consistency', None)
    report['validation']['narrative_reuse'] = {'report_file': str(previous_path.relative_to(ROOT)),
        'report_sha256': base.sha256(previous_path), 'retained_fields': list(RETAINED_FIELDS)}
    report['run_metadata'] = {**report['run_metadata'], 'model': config['text_model'], 'calls': [],
        'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'framework': framework(),
        'validated_facts_reused': True, 'revision': 'targeted_judgement_refresh'}
    report['related_review_href'] = Path(os.path.relpath(review_md, output)).as_posix()
    revise_judgement(report, run, config, execute)
    errors = narrative_reuse_errors(report) + report_audit_errors(report['draft'], report['validation']['report_audit'])
    if errors:
        raise ValueError('Reused narrative audit is invalid: ' + str(errors))
    write_json(run / 'sources' / f'{pair.project_id}.json', report['source'])
    write_json(run / 'facts' / f'{pair.project_id}.json', {'facts': report['facts'],
        'audit': report['validation']['facts_audit'], 'inherited_from': report['validation']['narrative_reuse']})
    write_json(run / 'reports' / f'{pair.project_id}.json', report)
    output.mkdir(parents=True, exist_ok=True)
    (output / filename(report['source']['project_name'])).write_text(render(report))
    print(pair.project_id + ': refreshed judgment saved', flush=True)
    return {'project_id': pair.project_id, 'project_name': report['source']['project_name'],
        'filename': filename(report['source']['project_name']), 'facts': len(report['facts']), 'calls': len(report['run_metadata']['calls'])}


def run_project(pair, run, output, config, execute, reuse_facts=False):
    if config.get('achievement_report_mode') == 'expression':
        from achievement_expression import run_project as expression_run
        return expression_run(pair, run, output, config, execute)
    prior_path = run / 'sources' / f'{pair.project_id}.json'
    prior_source = json.loads(prior_path.read_text()) if prior_path.exists() else None
    with call_scope(report='achievement', stage='extract'):
        source = sources.extract(pair)
    pid = pair.project_id; method = framework()
    write_json(run / 'sources' / f'{pid}.json', source)
    context = {'project_id': pid, 'project_name': source['project_name'],
        'framework_method': method['method_only'], 'input_policy': source['input_policy'],
        'source_records': [{'ref': ref, **record} for ref, record in source['records'].items()]}
    calls = []; feedback = []; facts = []; fact_audit = {}
    if reuse_facts:
        catalog = json.loads((run / 'facts' / f'{pid}.json').read_text())
        if prior_source != source:
            raise ValueError('Prior achievement source extraction changed; cannot reuse fact audit')
        facts, fact_audit = catalog['facts'], catalog['audit']
        _, errors = resolve_facts({'facts': facts}, source['records'])
        errors += audit_checks(fact_audit, 'fact_id', [f['fact_id'] for f in facts],
            ('supported', 'state_preserved', 'scope_preserved', 'conflicts_preserved'))
        prior_audit = json.loads((ROOT / catalog['calls'][-1]['cache_file']).read_text())
        if errors or prior_audit['request'].get('facts_draft') != facts or prior_audit['request'].get('framework_method') != method['method_only']:
            raise ValueError('Prior fact audit does not match current facts and framework method')
        calls = copy.deepcopy(catalog['calls'])
    for attempt in range(3):
        if reuse_facts:
            break
        raw, usage = cached_call(run, pid, f'facts-edit-{attempt}', prompts.FACTS,
            {**context, 'editing_feedback': feedback}, config, execute); calls.append({'stage': 'facts-edit', **usage})
        facts, errors = resolve_facts(raw, source['records'])
        if any(isinstance(error, str) and error.startswith(('fact_number_unsupported:', 'fact_quote_missing:', 'fact_no_source:')) for error in errors):
            raw = repair_fact_citations(raw, errors, source, run, pid, config, execute, attempt, calls)
            facts, errors = resolve_facts(raw, source['records'])
        fact_audit, usage = cached_call(run, pid, f'facts-audit-{attempt}', prompts.FACT_AUDIT,
            {**context, 'facts_draft': facts}, config, execute); calls.append({'stage': 'facts-audit', **usage})
        errors += audit_checks(fact_audit, 'fact_id', [f['fact_id'] for f in facts],
            ('supported', 'state_preserved', 'scope_preserved', 'conflicts_preserved'))
        if not errors:
            break
        feedback = {'errors': errors, 'previous_draft': raw,
            'instruction': '修订完整facts，修复引文/状态/范围/数字，保留其他有效内容；不新增材料以外事实。'}
    else:
        write_json(run / 'failed-facts' / f'{pid}.json', {'facts': facts, 'feedback': feedback})
        raise ValueError('Achievement facts did not pass source/semantic checks')
    write_json(run / 'facts' / f'{pid}.json', {'facts': facts, 'audit': fact_audit, 'calls': calls})
    checked = {'project_id': pid, 'project_name': source['project_name'], 'framework_method': method['method_only'],
        'checked_facts': facts, 'fact_source_context': {c['ref']: source['records'][c['ref']] for f in facts for c in f['citations']}}
    feedback = []; draft = {}; report_audit = {}
    for attempt in range(3):
        raw, usage = cached_call(run, pid, f'report-edit-{attempt}', prompts.WRITER,
            {**checked, 'editing_feedback': feedback}, config, execute); calls.append({'stage': 'report-edit', **usage})
        draft, errors = resolve_report(raw, facts)
        draft, errors = focus_bounded_judgement(draft, errors, facts, checked, run, pid,
                                               config, execute, attempt, calls)
        audit_requests = [
            ('report-audit', prompts.REPORT_AUDIT, {**checked, 'value_title': draft.get('value_title'), 'draft_statements': statements(draft)}),
            ('judgement-audit', prompts.JUDGEMENT_AUDIT, {**checked, 'value_title': draft.get('value_title'),
                'retained_narrative': retained_context(draft), 'draft_statements': [draft.get('overall_judgement', {})]})]
        def audit_request(entry):
            stage, prompt, payload = entry
            return cached_call(run, pid, f'{stage}-{attempt}', prompt, payload, config, execute)
        (report_audit, report_usage), (judgement_audit, judgement_usage) = parallel_map(
            audit_request, audit_requests, config.get('audit_workers', 1))
        calls += [{'stage':'report-audit', **report_usage}, {'stage':'judgement-audit', **judgement_usage}]
        errors += report_audit_errors(draft, report_audit)
        errors += judgement_audit_errors(draft, judgement_audit)
        if report_audit.get('title_supported') is not True:
            errors.append('report_title_unsupported')
        if not errors:
            break
        feedback = {'errors': errors, 'previous_draft': draft,
            'instruction': '修订完整报告。保持已核对事实和采用状态，不新增比例或事实；价值与建议用适当条件表达。'}
    else:
        write_json(run / 'failed-report' / f'{pid}.json', {'draft': draft, 'feedback': feedback})
        raise ValueError('Achievement narrative did not pass independent checks')
    report = {'schema_version': VERSION, 'project_id': pid, 'source': source, 'facts': facts, 'draft': draft,
        'validation': {'facts_audit': fact_audit, 'report_audit': report_audit, 'judgement_audit': judgement_audit, 'status': 'passed'},
        'run_metadata': {'model': config['text_model'], 'provider': config.get('provider', 'siliconflow'), 'framework': method,
            'calls': calls, 'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'input_files': [source['plan_file'], source['report_file']], 'external_retrieval': False,
            'validated_facts_reused': reuse_facts}}
    write_json(run / 'reports' / f'{pid}.json', report)
    output.mkdir(parents=True, exist_ok=True)
    (output / filename(source['project_name'])).write_text(render(report))
    print(pid + ': achievement report saved', flush=True)
    return {'project_id': pid, 'project_name': source['project_name'], 'filename': filename(source['project_name']),
        'facts': len(facts), 'calls': len(calls), 'successful_response_tokens': sum(c.get('total_tokens', 0) for c in calls)}


def check_paired_report(pair, run, output, review_dir, config, execute):
    path = run / 'reports' / f'{pair.project_id}.json'
    report = json.loads(path.read_text()); source = report['source']; facts = report['facts']
    review_path = review_dir / f'{pair.project_id}.json'; review = json.loads(review_path.read_text())
    from achievement_expression import is_expression, check_paired
    if is_expression(report):
        check_paired(report, run, output, review_path, config, execute)
        return {'project_id': pair.project_id, 'project_name': source['project_name'],
                'filename': filename(source['project_name']), 'paired_consistency_passed': True}
    for role, original in [('plan', pair.plan_path), ('report', pair.report_path)]:
        actual_hash = base.sha256(original)
        if source[role + '_sha256'] != actual_hash or review['review_scope'][role + '_sha256'] != actual_hash:
            raise ValueError('Paired reports do not use identical source files')
    checked = {'project_id': pair.project_id, 'project_name': source['project_name'],
        'framework_method': report['run_metadata']['framework']['method_only'], 'checked_facts': facts,
        'fact_source_context': {c['ref']: source['records'][c['ref']] for f in facts for c in f['citations']}}
    review_text = contract.render_markdown(review).split('## 附录：', 1)[0]
    before = contract.digest(report['draft']); calls = []
    for attempt in range(3):
        cross, usage = cached_call(run, pair.project_id, f'paired-consistency-{attempt}', prompts.CONSISTENCY,
            {**checked, 'review_report': review_text, 'achievement_report_statements': statements(report['draft'])}, config, execute)
        calls.append({'stage': 'paired-consistency', **usage})
        if cross.get('consistent') is True and cross.get('issues') == []:
            report['validation']['paired_report_consistency'] = {'passed': True, 'audit': cross,
                'review_report_file': str(review_path.relative_to(ROOT)), 'review_report_sha256': base.sha256(review_path),
                'original_draft_sha256': before, 'final_draft_sha256': contract.digest(report['draft'])}
            report['run_metadata']['calls'] += calls
            write_json(path, report)
            (output / filename(source['project_name'])).write_text(render(report))
            return {'project_id': pair.project_id, 'project_name': source['project_name'], 'filename': filename(source['project_name']),
                'facts': len(facts), 'paired_consistency_passed': True, 'calls': len(report['run_metadata']['calls']),
                'successful_response_tokens': sum(c.get('total_tokens', 0) for c in report['run_metadata']['calls'])}
        if attempt == 2:
            break
        if report['validation'].get('narrative_reuse'):
            revise_judgement(report, run, config, execute, f'paired-judgement-{attempt}',
                {'paired_review': review_text, 'consistency_issues': cross, 'previous_judgement': report['draft']['overall_judgement']})
            continue
        raw, usage = cached_call(run, pair.project_id, f'paired-report-repair-{attempt}', prompts.WRITER,
            {**checked, 'editing_feedback': {'paired_review': review_text, 'consistency_issues': cross,
                'previous_draft': report['draft'], 'instruction': '仅修复成果分析中与履约审查的实质冲突，保留已有事实。报告侧重点不同不等于冲突；保持具体成果范围。'}}, config, execute)
        calls.append({'stage': 'paired-report-repair', **usage}); draft, errors = resolve_report(raw, facts)
        audit, usage = cached_call(run, pair.project_id, f'paired-report-repair-audit-{attempt}', prompts.REPORT_AUDIT,
            {**checked, 'value_title': draft.get('value_title'), 'draft_statements': statements(draft)}, config, execute)
        calls.append({'stage': 'paired-report-repair-audit', **usage})
        errors += report_audit_errors(draft, audit)
        if errors or audit.get('title_supported') is not True:
            write_json(run / 'failed-consistency' / (pair.project_id + '.json'), {'cross': cross, 'errors': errors, 'audit': audit})
            raise ValueError('Cross-report revision did not pass source checks')
        report['draft'] = draft; report['validation']['report_audit'] = audit
        if 'overall_judgement' in draft:
            audit, usage = cached_call(run, pair.project_id, f'paired-judgement-audit-{attempt}', prompts.JUDGEMENT_AUDIT,
                {**checked, 'value_title': draft['value_title'], 'retained_narrative': retained_context(draft),
                 'draft_statements': [draft['overall_judgement']]}, config, execute)
            calls.append({'stage': 'paired-judgement-audit', **usage})
            if judgement_audit_errors(draft, audit):
                raise ValueError('Paired revision judgment failed independent audit')
            report['validation']['judgement_audit'] = audit
    write_json(run / 'failed-consistency' / (pair.project_id + '.json'), cross)
    raise ValueError('Paired reports still have an unresolved substantive conflict')


def export_reader_index(run, output, results):
    """A self-contained achievement compilation; existing review files are linked."""
    entries = [(row, json.loads((run / 'reports' / f"{row['project_id']}.json").read_text()))
               for row in sorted(results, key=lambda row: row['project_id'])]
    from achievement_expression import is_expression, export_reader_index as expression_index
    if entries and all(is_expression(report) for _, report in entries):
        expression_index(run, output, results)
        return
    if not entries or any('overall_judgement' not in report['draft'] for _, report in entries):
        return
    paired = all(report['validation'].get('paired_report_consistency', {}).get('passed') for _, report in entries)
    lines = ['# 软课题成果分析报告｜v0.2', '',
        '本版将推进态度收进“领域背景与成果价值”末尾的“综合判断”：用已有成果说明判断理由，并给出具体跟进方向。已移除单独的后续推进章节及汇报表达参考。', '',
        '模型：硅基流动 `deepseek-ai/DeepSeek-V3.2`。输入仍为同一对任务书和中期总结报告，未使用额外政策研究报告或外部检索。', '',
        '已核对的事实底稿、摘要、事实表及价值分析沿用上一版；综合判断由模型重新生成并独立复核。' +
        ('本批已完成与原履约报告的衔接检查。' if paired else '与履约报告的衔接检查尚未全部完成。'), '',
        '| 项目 | 新版成果分析 | 既有履约审查 |', '| --- | --- | --- |']
    compiled = ['# 软课题成果分析报告汇编', '', '本汇编包含本批新版成果报告正文及其完整判断依据。', '']
    for row, report in entries:
        href = report.get('related_review_href', '')
        review_link = f'[打开报告]({quote(href, safe="/")})' if href else '见原批次'
        lines.append(f"| {contract.cell(row['project_name'])} | [打开报告]({quote(row['filename'])}) | {review_link} |")
        compiled += ['---', '', render(report), '']
    lines += ['', '[连续阅读本批成果报告](三项目成果报告汇编.md)', '',
        '上一版成果报告及配套履约报告保留原文件，便于比较。']
    (output / 'README.md').write_text('\n'.join(lines) + '\n')
    (output / '三项目成果报告汇编.md').write_text('\n'.join(compiled))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, default=ROOT / 'runs/soft-achievements-v0.2')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'doc/三项软课题_成果分析_v0.2')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--project-id', action='append')
    parser.add_argument('--workers', type=int, choices=[1, 2, 3], default=2)
    parser.add_argument('--check-pairs-from', type=Path, help='Check existing achievement drafts against paired fulfillment report JSON; revise only if needed')
    parser.add_argument('--reuse-facts', action='store_true', help='Reuse matching independently audited fact catalog; rewrite/check narrative only')
    parser.add_argument('--refresh-judgement-from', type=Path, help='Reuse approved report JSON, facts and narrative; regenerate only overall judgment')
    parser.add_argument('--review-markdown-dir', type=Path, help='Existing fulfillment Markdown directory for links when refreshing')
    args = parser.parse_args()
    if args.refresh_judgement_from and (args.check_pairs_from or args.reuse_facts or not args.review_markdown_dir):
        parser.error('Judgment refresh requires --review-markdown-dir and cannot combine with other modes')
    if args.refresh_judgement_from and args.refresh_judgement_from.resolve() == (args.run_dir / 'reports').resolve():
        parser.error('Judgment refresh must use a new run directory')
    if not args.run_dir.resolve().is_relative_to(ROOT / 'runs'):
        parser.error('Private run directory must be inside this workspace runs directory')
    pairs = base.pair_sources(args.input_dir / '项目任务书', args.input_dir / '项目总结报告', None)
    if args.project_id:
        pairs = [p for p in pairs if p.project_id in set(args.project_id)]
        if {p.project_id for p in pairs} != set(args.project_id):
            parser.error('Unknown project ID')
    config = json.loads(base.CONFIG.read_text()); config['request_timeout_seconds'] = 360
    results, errors = [], {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        if args.refresh_judgement_from:
            futures = {pool.submit(refresh_judgement, p, args.run_dir.resolve(), args.output_dir.resolve(),
                       args.refresh_judgement_from.resolve(), args.review_markdown_dir.resolve(), config, args.execute): p for p in pairs}
        elif args.check_pairs_from:
            futures = {pool.submit(check_paired_report, p, args.run_dir.resolve(), args.output_dir.resolve(),
                       args.check_pairs_from.resolve(), config, args.execute): p for p in pairs}
        else:
            futures = {pool.submit(run_project, p, args.run_dir.resolve(), args.output_dir.resolve(), config, args.execute, args.reuse_facts): p for p in pairs}
        for future in as_completed(futures):
            pair = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                errors[pair.project_id] = type(error).__name__ + ': ' + str(error)
                print(pair.project_id + ': ' + errors[pair.project_id], flush=True)
            write_json(args.run_dir / 'progress.json', {'completed': results, 'errors': errors, 'expected': len(pairs)})
    write_json(args.run_dir / 'batch-manifest.json', {'version': VERSION, 'expected_projects': len(pairs),
        'batch_complete': not errors and len(results) == len(pairs), 'reports': sorted(results, key=lambda r: r['project_id']), 'errors': errors})
    if not errors:
        export_reader_index(args.run_dir.resolve(), args.output_dir.resolve(), results)
    if errors:
        raise SystemExit(str(len(errors)) + ' achievement report(s) failed')


if __name__ == '__main__':
    main()
