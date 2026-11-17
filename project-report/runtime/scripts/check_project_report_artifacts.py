#!/usr/bin/env python3
"""Check exported report completeness and literal provenance without model calls."""
import argparse
import copy
import json
import re
from collections import Counter
from pathlib import Path

import build_contract_review as base
import project_report_contract as contract
import project_report_presentation as presentation
import project_report_coordination as coordination
import project_report_assurance as assurance
import project_report_findings as findings
import project_report_synthesis as synthesis
import project_report_evidence as evidence_view
import project_report_followup as followup
import project_report_profile as profile

ROOT = Path(__file__).resolve().parent.parent


def check_report(report, markdown, source, root):
    errors = []
    import project_report_citations as citations
    compact_sources = citations.enabled(report)
    displayed_source_labels = citations.source_labels(report) if compact_sources else {}
    if compact_sources and markdown != contract.render_markdown(report):
        errors.append('centralized_citations_markdown_not_reproducible')
    ids = [item['item_id'] for item in report['plan_items']]
    alignments = [row['item_id'] for row in report['alignments']]
    if len(ids) != len(set(ids)) or Counter(ids) != Counter(alignments):
        errors.append('condition_alignment_coverage_mismatch')
    if report['manager_feedback'] != {'collection_status': 'not_started', 'overall': None, 'items': []}:
        errors.append('unexpected_feedback_before_collection')
    records = contract.report_sources(source)
    normalized_md = ' '.join(re.sub(r'(?m)^> ?', '', markdown).split())
    for entry in report['source_index']:
        raw = source['plan_blocks'].get(entry['ref'], '') if entry['side'] == 'plan' else records.get(entry['ref'], {}).get('text', '')
        if not contract.literal_quote(entry['quote'], raw):
            errors.append(f"source_quote_invalid:{entry['source_id']}")
    exported = report.get('presentation', {}).get('evidence_notes', {}).get('items', [])
    for entry in exported:
        raw = source['plan_blocks'].get(entry['ref'], '') if entry['side'] == 'plan' else records.get(entry['ref'], {}).get('text', '')
        if not contract.literal_quote(entry['quote'], raw) or not entry.get('semantic_reviewed') or not (entry.get('explanation') or entry.get('use_for')):
            errors.append(f"evidence_explanation_not_checked:{entry['source_id']}")
        if ' '.join(entry['quote'].split()) not in normalized_md:
            errors.append(f"source_quote_absent_from_markdown:{entry['source_id']}")
        if displayed_source_labels.get(entry['source_id'], entry['source_id']) not in markdown or entry['file_name'] not in markdown:
            errors.append(f"source_location_absent_from_markdown:{entry['source_id']}")
    for role in ('plan', 'report'):
        path = root / report['review_scope'][role + '_file']
        if not path.exists() or base.sha256(path) != report['review_scope'][role + '_sha256']:
            errors.append(f'{role}_file_hash_mismatch')
    if 'administrative_profile' in report:
        actual = profile.extract(root / report['review_scope']['plan_file'], report['project_id'], root)
        if report['administrative_profile'] != actual:
            errors.append('cover_fields_or_literal_provenance_mismatch')
        header = markdown.split('## 一、', 1)[0]
        if any(line not in header for line in profile.header_lines(actual) if line):
            errors.append('cover_fields_not_rendered')
    if report.get('source_document_links'):
        try:
            profile.validate_links({report['project_id']: report['source_document_links']})
        except ValueError:
            errors.append('invalid_material_links')
        for role in ('plan', 'report'):
            if profile.material_label(report, role) not in markdown:
                errors.append('material_link_not_rendered:' + role)
    by_id = {x['item_id']: x for x in report['plan_items']}
    if presentation.failed_current_comparison(report['plan_items'], report['alignments']):
        errors.append('current_comparison_lost_to_technical_validation')
    for row in report['alignments']:
        item = by_id[row['item_id']]
        if row['status'] in contract.ASSERTIVE and not row['semantic_reviewed']:
            errors.append('conclusive_state_without_validation:' + row['item_id'])
        if row['status'] == '后续节点' and item['checkpoint_scope'] != '后续节点':
            errors.append('current_scope_hidden_as_future:' + row['item_id'])
    for heading in ('一、项目概况', '二、计划与实际', '三、项目出入点与对接重点', '### 综合审查意见', '附录：判断依据'):
        if heading not in markdown:
            errors.append('missing_report_module:' + heading)
    if '### 综合审查意见' in markdown:
        if not markdown.index('## 三、') < markdown.index('### 综合审查意见') < markdown.index('## 附录：'):
            errors.append('conclusion_outside_findings_section')
    if '### 待确认事项' in markdown and not markdown.index('### 综合审查意见') < markdown.index('### 待确认事项') < markdown.index('## 附录：'):
        errors.append('pending_questions_outside_findings_section')
    if report.get('presentation'):
        p = report['presentation']
        grouped = report['validation'].get('grouped_composition')
        if grouped:
            import project_report_grouped
            if grouped.get('version') != project_report_grouped.VERSION or grouped.get('content_sha256') != project_report_grouped.content_digest(report):
                errors.append('grouped_review_content_changed_after_audit')
            _, progress_errors = project_report_grouped.guard_current_progress(copy.deepcopy(report['alignments']), records)
            errors += ['grouped_review_current_fact:' + str(value) for value in progress_errors]
        projected_view = p.get('pi_coordination', {})
        if projected_view.get('mode') == 'checked_alignment_projection':
            replay = copy.deepcopy(report)
            coordination.attach_checked_topics(replay, source, records)
            if replay['presentation']['pi_coordination'] != projected_view:
                errors.append('checked_coordination_projection_changed')
        if p.get('missing_sections'):
            errors.append('missing_reader_sections')
        discussion = p.get('pi_coordination')
        if discussion is None or discussion.get('missing_item_ids'):
            errors.append('missing_pi_coordination')
        elif Counter(key for item in discussion['items'] for key in item['item_ids']) != Counter(x['item_id'] for x in coordination.targets_for(report)):
            errors.append('pi_coordination_topic_coverage_mismatch')
        actual_ids = [row['item_id'] for row in report['alignments'] if presentation.visible_alignment(row)]
        if p.get('current_comparison_item_ids') != actual_ids:
            errors.append('reader_comparison_selection_mismatch')
        basis = p.get('acceptance_basis', {})
        if basis.get('missing_item_ids') or Counter(item['item_id'] for item in basis.get('items', [])) != Counter(actual_ids):
            errors.append('acceptance_basis_coverage_mismatch')
        if actual_ids and not basis.get('summary') and not basis.get('summary_pending'):
            errors.append('missing_acceptance_summary')
        for item in basis.get('items', []):
            if item.get('review_state') == 'needs_internal_review' and (item['basis_status'] != '待复核' or item.get('question')):
                errors.append('internal_review_presented_as_business_judgement')
            if item.get('scope_binding') == '适用范围待确认' and item.get('basis_status') in {'依据对应', '存在口径出入'}:
                errors.append('unbound_acceptance_rule_used_conclusively')
        conclusion = p.get('integrated_conclusion', {})
        paragraphs = conclusion.get('paragraphs', [])
        if conclusion.get('version') != synthesis.VERSION or not conclusion.get('semantic_reviewed') or not 1 <= len(paragraphs) <= 2 or any(value.get('kind') != 'narrative' for value in paragraphs):
            errors.append('integrated_conclusion_incomplete')
        else:
            _, synthesis_errors = synthesis.validate(conclusion, conclusion.get('audit', {}), synthesis.context_for(report, source, records))
            errors += ['integrated_conclusion:' + error for error in synthesis_errors]
        actual_conclusions = [value for value in report['overall_assessment']['statements'] if value['section'] == 'conclusion']
        if [value['text'] for value in actual_conclusions] != [value['text'] for value in paragraphs]:
            errors.append('active_conclusion_fields_not_synchronized')
        displayed_conclusion = markdown.split('### 综合审查意见', 1)[1].split('### 待确认事项', 1)[0].split('## 附录：', 1)[0]
        displayed_conclusion = displayed_conclusion.split('### ' + followup.PI_HEADING, 1)[0].split('**' + followup.MANAGER_LABEL + '**', 1)[0]
        if any(value['text'] not in displayed_conclusion for value in paragraphs):
            errors.append('integrated_conclusion_not_rendered')
        old_summary = (p.get('legacy_conclusion_inputs', {}).get('recognition_summary') or {}).get('text')
        if old_summary and old_summary in displayed_conclusion and old_summary not in [value['text'] for value in paragraphs]:
            errors.append('legacy_recognition_summary_still_appended')
        if p.get('pi_confirmation') is not None:
            errors += followup.validate_reuse(report)
            end = markdown.index('## 附录：')
            start = markdown.index('### 综合审查意见')
            before_conclusion = markdown.split('## 三、', 1)[1].split('### 综合审查意见', 1)[0]
            if '**与 PI 对接：**' in before_conclusion or '### 待确认事项' in markdown:
                errors.append('inline_or_duplicate_confirmation_section')
            pi_items = [value for value in p['pi_confirmation']['items'] if value['audience'] == 'pi']
            manager_items = [value for value in p['pi_confirmation']['items'] if value['audience'] == 'manager']
            for heading, values in [('### ' + followup.PI_HEADING, pi_items), ('**' + followup.MANAGER_LABEL + '**', manager_items)]:
                if values and (heading not in markdown or not start < markdown.index(heading) < end):
                    errors.append('centralized_confirmation_missing_or_misplaced')
                if not values and heading in markdown:
                    errors.append('empty_confirmation_heading')
            for value in p['pi_confirmation']['items']:
                if markdown[start:end].count(value['text']) != 1:
                    errors.append('confirmation_text_not_rendered_once:' + value['entry_id'])
        view = p.get('categorized_findings', {})
        topics, _ = presentation.review_topics(report)
        if view.get('missing_topic_ids') or Counter(key for topic in view.get('items', []) for key in topic['item_ids']) != Counter(key for topic in topics for key in topic['item_ids']):
            errors.append('categorized_findings_coverage_mismatch')
        for topic in view.get('items', []):
            if topic['category'] not in findings.CATEGORIES or any(not topic.get(field, {}).get('citations') for field in findings.FIELDS):
                errors.append('categorized_finding_incomplete')
        groups, _ = findings.evidence_context(report, source, records)
        if Counter(entry['source_id'] for entry in exported) != Counter(group['source_id'] for group in groups):
            errors.append('exported_evidence_coverage_mismatch')
        targets = re.findall(r'<a name="([^"]+)"', markdown)
        links = re.findall(r'\]\(#([^)]*)\)', markdown)
        if len(targets) != len(set(targets)) or any(target not in targets for target in links):
            errors.append('broken_or_duplicate_evidence_anchors')
        if p['evidence_notes'].get('version') == evidence_view.VERSION:
            rich_groups, rich_uses = evidence_view.context_for(report, source, records)
            _, evidence_errors = evidence_view.validate({'items': exported}, {'checks': [card['audit'] for card in exported]},
                                                       rich_groups, {card['source_id']: card for card in exported})
            errors += ['evidence_context:' + str(error) for error in evidence_errors]
            if p['evidence_notes']['uses'] != rich_uses:
                errors.append('evidence_usage_graph_mismatch')
            if markdown.count('**引用说明：**') != len(exported) or '**引用用途：**' in markdown:
                errors.append('missing_contextual_evidence_explanations')
            for card in exported:
                for extra in card.get('context_excerpts', []):
                    raw_context = records.get(extra['ref'], {}).get('text', '')
                    # A complete table cell such as “是” is meaningful context,
                    # although it is too short to stand alone as claim evidence.
                    exact_cell = bool(raw_context.strip()) and extra['quote'] == raw_context
                    if not (exact_cell or contract.literal_quote(extra['quote'], raw_context)) or ' '.join(extra['quote'].split()) not in normalized_md:
                        errors.append('context_excerpt_invalid_or_not_rendered:' + card['source_id'])
        elif markdown.count('**引用用途：**') != len(exported):
            errors.append('missing_legacy_evidence_purposes')
        if not exported or not links:
            errors.append('missing_evidence_links_or_purposes')
        second = markdown.split('## 二、', 1)[1].split('## 三、', 1)[0]
        third = markdown.split('## 三、', 1)[1].split('## 附录：', 1)[0]
        if any(word in second for word in ('尚待拆解', '本轮登记', '暂未形成可直接采用')):
            errors.append('internal_placeholder_in_comparison')
        if re.search(r'^\|', third, re.M):
            errors.append('confirmation_section_is_table')
        if '## 五、待确认' in markdown or '## 六、项目经理人反馈' in markdown:
            errors.append('old_pending_or_feedback_chapter')
        if '对当前审查的含义' in markdown:
            errors.append('old_follow_up_column')
        if re.search(r'^## [四五六]、|^## 附录A', markdown, re.M):
            errors.append('deprioritized_report_chapter_published')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, default=ROOT / 'runs' / 'project-reports-v0.11')
    parser.add_argument('--report-dir', type=Path, default=ROOT / 'doc' / '项目阶段履约审查报告_v0.11')
    parser.add_argument('--manifest', default='batch-manifest.json', choices=['batch-manifest.json', 'subset-manifest.json'])
    args = parser.parse_args()
    manifest = json.loads((args.run_dir / args.manifest).read_text())
    issues = {}
    expected_selection = 'subset' if args.manifest == 'subset-manifest.json' else 'all_pairs'
    if not manifest['batch_complete'] or manifest['selection'] != expected_selection or len(manifest['reports']) != manifest['expected_projects']:
        issues['batch'] = ['batch_incomplete']
    for row in manifest['reports']:
        pid = row['project_id']
        try:
            report = json.loads((args.run_dir / 'reports' / (pid + '.json')).read_text())
            source = json.loads((args.run_dir / 'sources' / (pid + '.json')).read_text())
            text = (args.report_dir / row['filename']).read_text()
            errors = check_report(report, text, source, ROOT)
            if errors:
                issues[pid] = errors
        except (KeyError, OSError, ValueError) as error:
            issues[pid] = [type(error).__name__]
    compilation = '试跑报告汇编.md' if expected_selection == 'subset' else '全部项目报告汇编.md'
    if manifest['batch_complete'] and not (args.report_dir / compilation).exists():
        issues['compilation'] = ['missing_complete_compilation']
    result = {'projects': len(manifest['reports']), 'passed': not issues, 'issues': issues,
              'note': 'Checks completeness, source hashes and literal provenance; not a semantic accuracy measurement.'}
    (args.run_dir / 'artifact-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if issues:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
