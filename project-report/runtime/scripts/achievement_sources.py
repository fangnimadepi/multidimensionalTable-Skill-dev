"""Extract achievement-relevant sections from one taskbook/report pair only."""
from pathlib import Path
import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

import build_contract_review as base
import project_report_profile as profile

VERSION = 'achievement-sources-v0.1.1'
PLAN_SECTIONS = (
    ('目标与问题', r'项目目标.*研究内容|研究内容.*关键科学'),
    ('研究方案与创新', r'研究方案.*创新'),
    ('成果与评测', r'成果形式.*考核|成果形式.*评测'),
    ('追加支持条件', r'阶梯追加|追加机制.*评估'),
    ('里程碑计划', r'里程碑.*年度'),
)
REPORT_HEADING = re.compile(r'^([一二三四五六七八九十]+)[、.．]\s*(.*)')
FINANCE = re.compile(r'经费|预算|财务')
# Personnel rosters are outside the report scope. Detect ID values even when
# a table heading and its values are split across rows.
PERSONAL_ID = re.compile(r'(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)')


def split_text(text, limit=1400):
    """Continuous chunks; preserve paragraph/row text without model rewriting."""
    if len(text) <= limit:
        return [text]
    parts, start = [], 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = max(text.rfind('。', start, end), text.rfind('\n', start, end))
            if boundary > start + limit // 2:
                end = boundary + 1
        parts.append(text[start:end]); start = end
    return parts


def docx_sections(path, side, plan_sections=PLAN_SECTIONS):
    doc = Document(path); records = {}; paragraph = table = 0; section = None

    def add(ref, text, label):
        if not text.strip() or PERSONAL_ID.search(text):
            return
        for index, chunk in enumerate(split_text(text), 1):
            key = ref if len(text) <= 1400 else ref + f'-S{index}'
            records[key] = {'side': side, 'file': str(path.relative_to(base.ROOT)),
                'location': ref.split('-', 1)[1], 'section': label, 'text': chunk}

    for block in doc.element.body:
        kind = block.tag.rsplit('}', 1)[-1]
        if kind == 'p':
            paragraph += 1; text = Paragraph(block, doc).text
            normalized = re.sub(r'\s+', '', text)
            if side == 'plan':
                hit = next((name for name, pattern in plan_sections if re.search(pattern, normalized)), None)
                if hit and len(normalized) < 80:
                    section = hit
                elif section and re.search(r'经费|预算|参加人员|人员情况|单位基本', normalized) and len(normalized) < 70:
                    section = None
            else:
                hit = REPORT_HEADING.match(normalized)
                if hit and len(normalized) < 65:
                    title = hit.group(2)
                    section = None if FINANCE.search(title) or title.startswith(('附件', '内容说明', '格式要求', '编制要求')) else title
                if paragraph < 27:
                    continue
            if section:
                add(('P' if side == 'plan' else 'R') + f'-P{paragraph}', text, section)
        elif kind == 'tbl':
            table += 1
            if not section:
                continue
            for ri, row in enumerate(Table(block, doc).rows, 1):
                cells, seen = [], set()
                for cell in row.cells:
                    if cell._tc in seen:
                        continue
                    seen.add(cell._tc); cells.append(cell.text)
                # Keep same-row comparison context, not detached completion cells.
                add(('P' if side == 'plan' else 'R') + f'-T{table}-R{ri}', '\n'.join(cells), section)
    return records


def pdf_sections(path):
    from pdf_ocr import read_pdf_pages
    pages, ocr = read_pdf_pages(path)
    records = pdf_page_sections(path, pages)
    if not records and not ocr['scanned']:
        # Some PDFs store a heading and the following body in one text object.
        # Layout extraction recovers visual lines while keeping original pages.
        pages = [page.extract_text(extraction_mode='layout') or '' for page in PdfReader(path).pages]
        records = pdf_page_sections(path, pages)
    return records, ocr['scanned']


def pdf_page_sections(path, pages):
    records = {}; section = None
    for index, page in enumerate(pages, 1):
        if index < 3:
            continue
        selected = []; section_labels = []
        for line in page.splitlines():
            compact = re.sub(r'\s+', '', line)
            hit = REPORT_HEADING.match(compact)
            if hit and len(compact) < 65:
                title = hit.group(2)
                if title.startswith(('内容说明', '格式要求', '编制要求')):
                    continue
                section = None if FINANCE.search(title) or title.startswith('附件') else title
            if section and not PERSONAL_ID.search(line):
                if section not in section_labels:
                    section_labels.append(section)
                selected.append(line)
        text = '\n'.join(selected)
        if not text.strip():
            continue
        for n, chunk in enumerate(split_text(text), 1):
            ref = f'R-PDF-P{index}' + (f'-S{n}' if len(text) > 1400 else '')
            records[ref] = {'side': 'report', 'file': str(path.relative_to(base.ROOT)),
                'location': f'PDF文件第{index}页', 'section': '／'.join(section_labels), 'text': chunk}
    return records


def extract(pair):
    plans = docx_sections(pair.plan_path, 'plan')
    if pair.report_path.suffix.lower() == '.pdf':
        reports, scanned = pdf_sections(pair.report_path)
    else:
        reports, scanned = docx_sections(pair.report_path, 'report'), False
    if not plans or not reports:
        raise ValueError('Achievement section extraction is empty')
    registry = profile.extract(pair.plan_path, pair.project_id, base.ROOT)
    return {'version': VERSION, 'project_id': pair.project_id, 'project_name': pair.plan_path.stem,
        'plan_file': str(pair.plan_path.relative_to(base.ROOT)), 'report_file': str(pair.report_path.relative_to(base.ROOT)),
        'plan_sha256': base.sha256(pair.plan_path), 'report_sha256': base.sha256(pair.report_path),
        'administrative_profile': registry, 'scanned_report': scanned, 'records': {**plans, **reports},
        'input_policy': 'Only paired taskbook and interim report; no extra research report, external retrieval or example-project facts'}
