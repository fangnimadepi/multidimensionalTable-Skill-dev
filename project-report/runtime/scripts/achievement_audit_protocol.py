"""Separate incomplete audit responses from findings about report content."""
from __future__ import annotations

import copy
import json


def grouped_checks(audit):
    groups = {}
    if not isinstance(audit, dict):
        return groups
    values = audit.get('checks', [])
    if isinstance(values, list):
        for value in values:
            if isinstance(value, dict) and isinstance(value.get('statement_id'), str):
                groups.setdefault(value['statement_id'], []).append(value)
    return groups


def coverage_targets(audit, expected):
    expected = set(expected)
    if not isinstance(audit, dict):
        return expected
    groups = grouped_checks(audit)
    targets = {key for key in expected if len(groups.get(key, [])) != 1
               or not isinstance(groups[key][0].get('material_fact_ok'), bool)}
    issues = audit.get('material_issues')
    checks = audit.get('checks')
    malformed = (not isinstance(checks, list)
                 or any(not isinstance(row, dict) or row.get('statement_id') not in expected for row in checks)
                 or not isinstance(issues, list)
                 or any(not isinstance(row, dict) or row.get('statement_id') not in expected for row in (issues or []))
                 or not isinstance(audit.get('paired_consistent'), bool))
    # A malformed global result needs a complete protocol clarification. Known
    # negative findings below are retained, even if the clarification disagrees.
    return expected if malformed else targets


def merge_coverage(primary, supplement, expected, targets):
    """Complete missing checks without letting a second pass erase a rejection."""
    primary = primary if isinstance(primary, dict) else {}
    supplement = supplement if isinstance(supplement, dict) else {}
    result = copy.deepcopy(primary)
    old, new = grouped_checks(primary), grouped_checks(supplement)
    result['checks'] = []
    for key in expected:
        entries = new.get(key, []) if key in targets else old.get(key, [])
        if key in targets and any(row.get('material_fact_ok') is False for row in old.get(key, [])):
            result['checks'].append({'statement_id': key, 'material_fact_ok': False,
                'reason': '保留原审核中明确提出的实质问题；补齐编号不能撤销该问题。'})
        else:
            result['checks'].extend(copy.deepcopy(entries))
    issues, seen = [], set()
    for audit in (primary, supplement):
        for value in audit.get('material_issues', []) if isinstance(audit.get('material_issues'), list) else []:
            if not isinstance(value, dict) or value.get('statement_id') not in expected:
                continue
            key = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                issues.append(copy.deepcopy(value)); seen.add(key)
    result['material_issues'] = issues
    if primary.get('paired_consistent') is False or supplement.get('paired_consistent') is False:
        result['paired_consistent'] = False
    elif not isinstance(primary.get('paired_consistent'), bool):
        result['paired_consistent'] = supplement.get('paired_consistent')
    result['style_suggestions'] = list(primary.get('style_suggestions') or [])
    return result


def content_targets(audit, expected):
    expected = set(expected)
    result = {row['statement_id'] for values in grouped_checks(audit).values() for row in values
              if row.get('material_fact_ok') is False and row['statement_id'] in expected}
    result.update(row['statement_id'] for row in audit.get('material_issues', [])
                  if isinstance(row, dict) and row.get('statement_id') in expected)
    return result
