"""Presentation-only centralized sources; all audited prose and evidence survive."""
from __future__ import annotations

import re

import project_report_contract as c
import project_report_findings as findings
import project_report_presentation as presentation

LAYOUT = 'centralized-sources-v1'


def enabled(report):
    return report.get('presentation', {}).get('citation_layout') == LAYOUT


def source_labels(report):
    cards = report.get('presentation', {}).get('evidence_notes', {}).get('items', report['source_index'])
    return {card['source_id']: f'来源{i:02d}' for i, card in enumerate(cards, 1)}


def anchor(report, name):
    return findings.anchor(report['project_id'], '') + name


def remove_inline_refs(text):
    link = r'\[依据\d+\]\(#evidence-[a-z0-9-]+\)'
    return re.sub(r'（' + link + r'(?:、' + link + r')*）', '', text)


def trim_comparison_column(body):
    old = '| 序号 | 任务书要求 | 计划节点 | 报告进展 | 完成情况及认定依据 | 来源 |'
    lines = []; active = False
    for line in body.splitlines():
        if line == old:
            active = True
        elif active and not line.startswith('|'):
            active = False
        if active:
            cells = re.split(r'(?<!\\)\|', line.strip())[1:-1]
            if len(cells) != 6:
                raise ValueError('Unexpected comparison row; refusing to drop content')
            line = '| ' + ' | '.join(cell.strip() for cell in cells[:-1]) + ' |'
        lines.append(line)
    return '\n'.join(lines)


def render(report, legacy_markdown):
    body, appendix = legacy_markdown.split('## 附录：', 1)
    pres = report['presentation']; cards = pres.get('evidence_notes', {}).get('items', report['source_index'])
    labels = source_labels(report); card_by_id = {v['source_id']: v for v in cards}
    aliases = {alias: card['source_id'] for card in cards for alias in card.get('alias_ids', [card['source_id']])}
    lookup = {(v['ref'], v['quote']): aliases.get(v['source_id'], v['source_id']) for v in report['source_index']}
    items = {v['item_id']: v for v in report['plan_items']}

    def refs(value):
        citations = value.get('citations', [])
        if not citations:
            citations = [{'ref': items[key]['source_ref'], 'quote': items[key]['quote']}
                         for key in value.get('item_ids', []) if key in items]
        found = []
        for cite in citations:
            sid = lookup.get((cite['ref'], cite['quote']))
            if sid not in card_by_id:
                raise ValueError('Displayed source binding has no evidence card')
            if sid not in found: found.append(sid)
        return found

    def links(ids, side):
        return '、'.join(f'[{labels[sid]}](#{findings.anchor(report["project_id"], sid)})'
                        for sid in dict.fromkeys(ids) if card_by_id[sid]['side'] == side)

    index = []
    def index_row(label, ids):
        if ids: index.append('| ' + c.cell(label) + ' | ' + links(ids, 'plan') + ' | ' + links(ids, 'report') + ' |')

    for key, label in [('overview', '项目概况'), ('plan_summary', '任务书计划'), ('achievements', '实际进展'), ('conclusion', '综合审查意见')]:
        index_row(label, [sid for v in report['overall_assessment']['statements'] if v['section'] == key for sid in refs(v)])
    bases = {v['item_id']: v for v in pres.get('acceptance_basis', {}).get('items', [])}
    for i, row in enumerate((r for r in report['alignments'] if presentation.visible_alignment(r)), 1):
        item = items[row['item_id']]
        cites = [{'ref': item['source_ref'], 'quote': item['quote']}] + row.get('evidence', [])
        cites += bases.get(row['item_id'], {}).get('citations', [])
        index_row(f'计划与进展对照·第{i}项', refs({'citations': cites}))
    for value in pres.get('pi_confirmation', {}).get('items', []):
        index_row('确认事项：' + value['title'], refs(value))

    # Rebuild only the issue block from the exact audited fields. Other prose
    # stays byte-for-byte identical after removing generated citation suffixes.
    topics, pending = presentation.review_topics(report); groups = []; topic_lines = []
    categorized = pres.get('categorized_findings')
    if categorized is not None:
        categories = [cat for cat in findings.CATEGORIES if any(t['category'] == cat for t in categorized['items'])]
        for ci, cat in enumerate(categories):
            topic_lines += [f'### （{"一二三四五"[ci]}）{cat}', '']
            for ti, topic in enumerate((t for t in categorized['items'] if t['category'] == cat), 1):
                suffix = '（待确认）' if topic['certainty'] == '待确认' else ''
                topic_lines += [f'#### {ti}. {topic["title"]}{suffix}', '']
                ids = []
                for field in findings.FIELDS:
                    value = topic[field]; ids += refs(value)
                    if field == 'discuss_with_pi' and pres.get('pi_confirmation') is not None: continue
                    topic_lines += [f'**{findings.FIELD_LABELS[field]}：** {value["text"]}', '']
                groups.append((topic['title'], list(dict.fromkeys(ids))))
                topic_lines += ['[查看本项依据](#' + anchor(report, f'finding-{len(groups):02d}') + ')', '']
    else:
        for i, topic in enumerate(topics, 1):
            ids = refs(topic); groups.append((topic['title'], ids))
            topic_lines += [f'### {i}. {topic["title"]}', '', '**出入情况：** ' + topic['difference'], '']
            if pres.get('pi_confirmation') is None: topic_lines += ['**与 PI 对接：** ' + topic['discuss_with_pi'], '']
            topic_lines += ['[查看本项依据](#' + anchor(report, f'finding-{len(groups):02d}') + ')', '']
    if groups:
        for _, ids in groups:
            if not links(ids, 'plan') or not links(ids, 'report'):
                raise ValueError('A finding must retain both taskbook and report sources')
        before, third = body.split('## 三、项目出入点与对接重点', 1)
        _, conclusion = third.split('### 综合审查意见', 1)
        body = before + '## 三、项目出入点与对接重点\n\n' + '\n'.join(topic_lines) + '\n### 综合审查意见' + conclusion

    body = trim_comparison_column(remove_inline_refs(body))
    body = body.replace('- 文中“依据”编号可在本报告末尾查阅文件位置及原文。',
        '\n[查看资料来源](#' + anchor(report, 'sources') + ')')
    # Keep all original excerpts, explanations, related evidence and use labels.
    appendix = appendix.split('\n', 1)[1].lstrip()
    for sid, label in labels.items():
        appendix = re.sub(r'(?m)^### ' + re.escape(sid) + r'｜', '#### ' + label + '｜', appendix)
        appendix = appendix.replace('[' + sid + '](', '[' + label + '](')
    lines = ['## 附录：判断依据与原文摘录', '',
        f'<a name="{anchor(report, "sources")}" id="{anchor(report, "sources")}"></a>', '',
        '### 审查内容与材料位置', '', '| 正文位置 | 任务书来源 | 总结报告来源 |', '| --- | --- | --- |'] + index + ['']
    if groups:
        lines += ['### 重点出入事项来源', '']
        for i, (title, ids) in enumerate(groups, 1):
            target = anchor(report, f'finding-{i:02d}')
            lines += [f'<a name="{target}" id="{target}"></a>', '', f'#### {i}. {title}', '',
                      '任务书：' + links(ids, 'plan'), '', '总结报告：' + links(ids, 'report'), '']
    lines += ['### 来源摘录', '', appendix]
    result = body.rstrip() + '\n\n' + '\n'.join(lines)
    from feishu_report_bundle import check_links
    check_links(result)
    return result
