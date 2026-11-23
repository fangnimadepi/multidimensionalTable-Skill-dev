"""Literal cover metadata and optional human-facing source links; no model calls."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from docx import Document

VERSION = 'project-report-profile-v0.11.0'
LABELS = {
    '项目编号': 'project_number',
    '项目承担单位': 'organization', '承担单位': 'organization',
    '项目责任人': 'principal_investigator', '项目负责人': 'principal_investigator',
    '项目经理人': 'project_manager',
    '项目执行期': 'execution_period', '项目起止日期': 'execution_period',
}
DISPLAY = (
    ('project_number', '项目编号'), ('organization', '承担单位'),
    ('principal_investigator', '项目负责人'), ('project_manager', '项目经理人'),
    ('execution_period', '项目执行期'),
)
LABEL_PATTERN = re.compile(r'^\s*(' + '|'.join(r'\s*'.join(label) for label in LABELS) + r')\s*[:：]?\s*(.*?)\s*$')
PRIVATE_LABEL = re.compile(r'通讯地址|联系电话|电子邮箱|电子邮件|身份证|手机号码')


def cover_lines(path):
    """Read the cover in document order, stopping before instructions/main text."""
    doc = Document(path)
    seen = 0
    paragraph = table = 0
    for block in doc.element.body:
        kind = block.tag.rsplit('}', 1)[-1]
        if kind == 'p':
            paragraph += 1
            from docx.text.paragraph import Paragraph
            units = [(f'正文段落 P{paragraph}（封面）', Paragraph(block, doc).text)]
        elif kind == 'tbl':
            table += 1
            from docx.table import Table
            units = [(f'表 T{table} 第{index}行（封面）', '：'.join(cell.text for cell in row.cells))
                     for index, row in enumerate(Table(block, doc).rows, 1)]
        else:
            continue
        for location, text in units:
            if '填写说明' in re.sub(r'\s+', '', text):
                return
            seen += 1
            if seen > 40:
                return
            # Keep the exact source line, not a reconstructed paraphrase.
            for line in text.splitlines():
                yield location, line


def extract(path, project_id, root):
    path, root = Path(path), Path(root)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        filename = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        filename = str(path.resolve())
    candidates = {}
    for location, quote in cover_lines(path):
        match = LABEL_PATTERN.fullmatch(quote)
        if not match or PRIVATE_LABEL.search(match.group(2)):
            continue
        label = re.sub(r'\s+', '', match.group(1))
        value = re.sub(r'\s*[（(](?:盖章|签章|签字|签名)[）)]\s*$', '', match.group(2)).strip(' ：:')
        value = re.sub(r'\s+', ' ', value)
        if not value:
            continue
        key = LABELS[label]
        if key == 'project_number':
            if project_id is None:
                continue
            value = re.sub(r'\s+', '', value)
            if value != project_id:
                raise ValueError('Taskbook cover project number does not match paired project')
        candidates.setdefault(key, []).append({'value': value, 'source_label': label,
            'source': {'file': filename, 'sha256': digest, 'location': location, 'quote': quote}})
    fields, conflicts = {}, {}
    for key, values in candidates.items():
        if len({entry['value'] for entry in values}) != 1:
            conflicts[key] = values
        else:
            fields[key] = {**values[0], 'sources': [entry['source'] for entry in values]}
    return {'version': VERSION, 'source_file': filename, 'source_sha256': digest,
            'fields': fields, 'conflicts': conflicts}


def validate_links(projects):
    """Accept explicit stable HTTPS sharing URLs, separately for each file role."""
    if not isinstance(projects, dict):
        raise ValueError('Source link projects must be an object')
    result = {}
    for project_id, links in projects.items():
        if not isinstance(links, dict) or set(links) - {'plan', 'report'}:
            raise ValueError('Source links require separate plan/report roles')
        selected = {}
        for role, url in links.items():
            if url == '':
                continue
            if not isinstance(url, str):
                raise ValueError('Source sharing URL must be text')
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if not valid or re.search(r'[\s<>]', url):
                raise ValueError('Source sharing URL must be a full HTTPS URL')
            if set(key.lower() for key in parse_qs(parsed.query)) & {'access_token', 'tenant_access_token', 'user_access_token', 'authorization'}:
                raise ValueError('Use a sharing URL, not an authenticated download API URL')
            selected[role] = url
        result[project_id] = selected
    return result


def load_links(path):
    if path is None:
        return {}
    data = json.loads(Path(path).read_text())
    return validate_links(data['projects'])


def header_lines(profile):
    fields = profile.get('fields', {})
    lines = ['| 项目信息 | 内容 |', '| --- | --- |']
    for key, label in DISPLAY:
        if key in fields:
            value = fields[key]['value'].replace('|', '\\|').replace('\n', ' ')
            lines.append(f'| {label} | {value} |')
    if len(lines) == 2:
        return []
    return lines + ['', '项目信息据任务书封面整理。', '']


def material_label(report, role):
    filename = Path(report['review_scope'][role + '_file']).name
    url = report.get('source_document_links', {}).get(role)
    if not url:
        return filename
    label = filename.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
    return f'[{label}](<{url}>)'
