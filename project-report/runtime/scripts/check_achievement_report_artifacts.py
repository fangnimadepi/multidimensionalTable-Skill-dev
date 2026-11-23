#!/usr/bin/env python3
"""Offline integrity and source checks for achievement/value report exports."""
import argparse
import json
import re
from pathlib import Path

import build_achievement_reports as reports
import build_contract_review as base
import achievement_sources as sources
import project_report_contract as contract


def check(report, markdown, root, require_paired=False):
    from achievement_expression import is_expression, check as expression_check
    if is_expression(report):
        return expression_check(report, markdown, root, require_paired)
    errors = []; source = report['source']; records = source['records']
    expected = {source['plan_file'], source['report_file']}
    if len(expected) != 2 or set(report['run_metadata']['input_files']) != expected:
        errors.append('not_exactly_two_paired_inputs')
    if any(value['file'] not in expected for value in records.values()):
        errors.append('unapproved_source_file')
    for role in ('plan', 'report'):
        if base.sha256(root / source[role + '_file']) != source[role + '_sha256']:
            errors.append('source_hash_changed:' + role)
    fresh = sources.extract(base.Pair(report['project_id'], root / source['plan_file'], root / source['report_file']))
    if fresh != source:
        errors.append('source_sections_not_reproducible_from_originals')
    _, fact_errors = reports.resolve_facts({'facts': report['facts']}, records)
    errors += fact_errors
    validation = report['validation']
    errors += reports.audit_checks(validation['facts_audit'], 'fact_id', [f['fact_id'] for f in report['facts']],
        ('supported', 'state_preserved', 'scope_preserved', 'conflicts_preserved'))
    _, report_errors = reports.resolve_report(report['draft'], report['facts']); errors += report_errors
    errors += reports.report_audit_errors(report['draft'], validation['report_audit'])
    if report.get('schema_version') == reports.VERSION and 'overall_judgement' not in report['draft']:
        errors.append('current_report_missing_judgement')
    if 'overall_judgement' in report['draft']:
        errors += reports.judgement_audit_errors(report['draft'], validation.get('judgement_audit', {}))
    errors += reports.narrative_reuse_errors(report, root)
    if validation['report_audit'].get('title_supported') is not True:
        errors.append('title_not_reviewed')
    if require_paired:
        paired = validation.get('paired_report_consistency', {})
        if (paired.get('passed') is not True or
            paired.get('final_draft_sha256') != contract.digest(report['draft']) or
            base.sha256(root / paired['review_report_file']) != paired['review_report_sha256']):
            errors.append('paired_consistency_missing_or_outdated')
    if reports.render(report) != markdown:
        errors.append('markdown_not_reproducible')
    targets = re.findall(r'<a [^>]*id="([^"]+)"', markdown)
    links = re.findall(r'\]\(#([^\)]+)\)', markdown)
    if len(targets) != len(set(targets)) or not set(links) <= set(targets):
        errors.append('broken_or_duplicate_anchor')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, default=base.ROOT / 'runs/soft-achievements-v0.2')
    parser.add_argument('--output-dir', type=Path, default=base.ROOT / 'doc/三项软课题_成果分析_v0.2')
    parser.add_argument('--require-paired', action='store_true')
    args = parser.parse_args(); manifest = json.loads((args.run_dir / 'batch-manifest.json').read_text())
    issues = {}
    for row in manifest['reports']:
        report = json.loads((args.run_dir / 'reports' / (row['project_id'] + '.json')).read_text())
        errors = check(report, (args.output_dir / row['filename']).read_text(), base.ROOT, args.require_paired)
        if errors:
            issues[row['project_id']] = errors
    result = {'reports': len(manifest['reports']), 'passed': manifest['batch_complete'] and not issues, 'issues': issues}
    reports.write_json(args.run_dir / 'artifact-check.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
