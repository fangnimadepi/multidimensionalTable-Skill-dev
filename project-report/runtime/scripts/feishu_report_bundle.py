"""Presentation-only bundle of already checked reports; never call a model."""
from __future__ import annotations

import hashlib
import re

VERSION = 'feishu-report-bundle-v2-pdf'
TYPES = {'review': '项目阶段履约审查报告', 'achievement': '项目阶段成果与后续推进分析报告'}
REPORT_TITLES = {**TYPES, 'forecast': '预期成果分析（任务书版）'}
TITLE = '项目阶段审查与成果分析报告'


def split_evidence(text):
    match = re.search(r'(?m)^## 附录[：:]', text)
    if not match:
        raise ValueError('Report has no evidence appendix')
    body = text[:match.start()].rstrip()
    body = re.sub(r'(?m)^- \[配套项目阶段履约审查报告\].*\n?', '', body)
    body = body.replace('文中“依据”编号可在本报告末尾查阅文件位置及原文。',
                        '文中“依据”编号可在依据资料部分查阅文件位置及原文。')
    return body, text[match.start():].strip()


def without_profile(body):
    """Remove only the known cover table; preserve all substantive report tables."""
    lines = body.splitlines(); output = []; skipping = False
    for line in lines:
        if re.fullmatch(r'\|\s*项目信息\s*\|\s*内容\s*\|', line):
            skipping = True
            continue
        if skipping and line.startswith('|'):
            continue
        skipping = False
        if line.strip() == '项目信息据任务书封面整理。':
            continue
        output.append(line)
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(output)).strip()


def nested(text, strip_title=False):
    if strip_title:
        text = re.sub(r'^# [^\n]+\n*', '', text, count=1)
        text = re.sub(r'(?m)^\*\*项目：[^\n]*\*\*\s*\n', '', text, count=1)
    return re.sub(r'(?m)^(#{1,5}) ', r'#\1 ', text).strip()


def achievement_after_review(body):
    """Keep the standalone fact sheet intact; omit its duplicate combined display."""
    body = re.sub(r'(?ms)^## 一、事实成果提炼\s*\n.*?(?=^## 二、成果表达\s*$)', '', body)
    body = body.replace('## 二、成果表达', '## 成果表达', 1)
    body = re.sub(r'(?m)^信息来源：本项目任务书与中期总结报告。\s*\n', '', body)
    body = re.sub(r'(?m)^- (?:任务书|总结报告)：[^\n]*\n?', '', body)
    return re.sub(r'\n{3,}', '\n\n', body).strip()


def check_links(markdown):
    anchors = re.findall(r'<a\b[^>]*\bid="([^"]+)"', markdown)
    if len(anchors) != len(set(anchors)):
        raise ValueError('Duplicate evidence anchors in combined report')
    missing = set(re.findall(r'\]\(#([^)]+)\)', markdown)) - set(anchors)
    if missing:
        raise ValueError('Missing combined report evidence anchors: ' + ', '.join(sorted(missing)))
    if re.search(r'\]\((?:[^)#]*\.md|\.\./)[^)]*\)', markdown):
        raise ValueError('A workspace or cross-file Markdown link cannot be published')


def display_markdown(project_name, results, evidence):
    """Reassemble the exact exported Markdown from the sidebar's three views."""
    parts = []
    for kind, title in TYPES.items():
        if kind in results:
            parts.append(f'## 第{"一二"[len(parts)]}部分 {title}\n\n' + nested(results[kind]['body'], strip_title=True))
    if not parts or not evidence.startswith('# 依据资料\n\n'):
        raise ValueError('报告正文或依据资料不完整。')
    markdown = f'# {TITLE}\n\n**项目：{project_name}**\n\n' + '\n\n'.join(parts)
    markdown += f'\n\n## 第{"二三"[len(parts) - 1]}部分 依据\n\n' + evidence[len('# 依据资料\n\n'):] + '\n'
    check_links(markdown)
    return markdown


def build_bundle(project_id, project_name, reports):
    """Use canonical order even if the user selects the checkboxes in reverse."""
    if not reports or set(reports) - set(TYPES):
        raise ValueError('One or both known report types are required')
    results = {}; appendices = []
    for kind, title in TYPES.items():
        if kind not in reports:
            continue
        body, appendix = split_evidence(reports[kind])
        # First visible report owns the project metadata, including achievement-only mode.
        if results:
            body = without_profile(body)
            if kind == 'achievement' and 'review' in results:
                body = achievement_after_review(body)
        results[kind] = {'title': title, 'body': body}
        appendix = re.sub(r'^## 附录[：:][^\n]*\n*', '', appendix, count=1)
        appendices.append('### ' + ('履约审查依据' if kind == 'review' else '成果分析依据') + '\n\n' + nested(appendix))
    evidence = '# 依据资料\n\n' + '\n\n'.join(appendices)
    markdown = display_markdown(project_name, results, evidence)
    return {'schemaVersion': VERSION, 'results': results, 'evidence': evidence,
            'bundle': {'title': TITLE, 'filename': f'{project_id}_{TITLE}.md', 'markdown': markdown,
                       'sha256': hashlib.sha256(markdown.encode()).hexdigest()}}


def standalone_reports(reports):
    """Each exported report owns its body, project details and link targets."""
    if not reports or set(reports) - set(REPORT_TITLES):
        raise ValueError('Unknown report type')
    for kind in REPORT_TITLES:
        if kind in reports:
            body, appendix = split_evidence(reports[kind])
            markdown = body + '\n\n' + appendix + '\n'
            check_links(markdown)
            yield kind, markdown
