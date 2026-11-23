"""Render checked reader copies as editable Word with plain source locations."""

import hashlib
import html
import json
from pathlib import Path
import re
import sys
from zipfile import ZipFile

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from reading_copy import CARD_END, CARD_START, without_navigation

LAYOUT = json.loads((Path(__file__).resolve().parent.parent / "templates/document-layout.json").read_text())
TEXT_FONT = "Microsoft YaHei" if sys.platform == "win32" else "Arial Unicode MS" if sys.platform == "darwin" else "Noto Sans CJK SC"


def bookmark_name(value):
    return "j" + hashlib.sha256(value.encode()).hexdigest()[:28]


def set_font(run, size=None, bold=False, color=None):
    run.font.name = TEXT_FONT
    run.font.color.rgb = RGBColor.from_string(color or LAYOUT["text_color"])
    run.bold = bold
    if size:
        run.font.size = Pt(size)
    fonts = run._element.get_or_add_rPr().rFonts
    fonts.set(qn("w:eastAsia"), TEXT_FONT)


def text_runs(paragraph, text, color=None):
    pattern = r'\[([^\]\n]+)\]\((#[A-Za-z0-9_-]+|https://[^\s)]+)\)|\*\*((?:(?!\*\*).)+)\*\*|`([^`]+)`|<br\s*/?>'
    end = 0
    for match in re.finditer(pattern, text):
        set_font(paragraph.add_run(html.unescape(text[end:match.start()].replace('\\|', '|'))), color=color)
        label, target, strong, code = match.groups()
        if target:
            hyperlink = OxmlElement("w:hyperlink")
            if target.startswith("#"):
                hyperlink.set(qn("w:anchor"), bookmark_name(target[1:]))
            else:
                from docx.opc.constants import RELATIONSHIP_TYPE

                relationship = paragraph.part.relate_to(target, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
                hyperlink.set(qn("r:id"), relationship)
            run = paragraph.add_run(html.unescape(label))
            set_font(run, color=LAYOUT["link_color"])
            run.underline = True
            hyperlink.append(run._element)
            paragraph._p.append(hyperlink)
        elif strong:
            start = len(paragraph.runs)
            text_runs(paragraph, strong, color=color)
            for run in paragraph.runs[start:]:
                run.bold = True
        elif code:
            set_font(paragraph.add_run(html.unescape(code)), color=color)
        else:
            paragraph.add_run().add_break()
        end = match.end()
    set_font(paragraph.add_run(html.unescape(text[end:].replace('\\|', '|'))), color=color)


def add_bookmarks(paragraph, names, counter):
    for name in names:
        counter[0] += 1
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), str(counter[0]))
        start.set(qn("w:name"), bookmark_name(name))
        finish = OxmlElement("w:bookmarkEnd")
        finish.set(qn("w:id"), str(counter[0]))
        paragraph._p.append(start)
        paragraph._p.append(finish)


def section_setup(section, landscape=False):
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    width, height = LAYOUT["landscape_cm" if landscape else "portrait_cm"]
    section.page_width, section.page_height = Cm(width), Cm(height)
    section.top_margin = section.bottom_margin = Cm(LAYOUT["margin_vertical_cm"])
    section.left_margin = section.right_margin = Cm(LAYOUT["margin_horizontal_cm"])


def table(document, rows, appendix=False, counter=None):
    if not rows or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("Word表格列数不一致，停止导出。")
    result = document.add_table(rows=0, cols=len(rows[0]))
    result.autofit = False
    available = LAYOUT["portrait_cm"][0] - 2 * LAYOUT["margin_horizontal_cm"]
    weights = {2: [0.26, 0.74], 3: [0.25, 0.35, 0.40],
               5: [0.045, 0.28, 0.12, 0.28, 0.275]}.get(len(rows[0]), [1 / len(rows[0])] * len(rows[0]))
    for column, weight in zip(result.columns, weights):
        column.width = Cm(available * weight)
    props = result._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        line = OxmlElement("w:" + name)
        for key, value in (("val", "single"), ("sz", "4"), ("color", LAYOUT["table_border"])):
            line.set(qn("w:" + key), value)
        borders.append(line)
    props.append(borders)
    for index, values in enumerate(rows):
        row = result.add_row()
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        if index == 0:
            repeat = OxmlElement("w:tblHeader")
            row._tr.get_or_add_trPr().append(repeat)
        for column_index, (cell, value, weight) in enumerate(zip(row.cells, values, weights)):
            cell.width = Cm(available * weight)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            properties = cell._tc.get_or_add_tcPr()
            margins = OxmlElement("w:tcMar")
            for side in ("top", "left", "bottom", "right"):
                margin = OxmlElement("w:" + side)
                margin.set(qn("w:w"), "80")
                margin.set(qn("w:type"), "dxa")
                margins.append(margin)
            properties.append(margins)
            if index == 0:
                shade = OxmlElement("w:shd")
                shade.set(qn("w:fill"), LAYOUT["table_header_fill"])
                properties.append(shade)
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.line_spacing = 1.2
            names = re.findall(r'<a id="([^"]+)"></a>', value)
            if names:
                add_bookmarks(paragraph, names, counter)
                value = re.sub(r'<a id="[^"]+"></a>', '', value)
            text_runs(paragraph, value, color=LAYOUT["heading_color"] if index == 0 else None)
            for run in paragraph.runs:
                run.font.size = Pt(9 if appendix or len(rows[0]) > 3 else 10)
                run.bold = index == 0 or (len(values) == 2 and column_index == 0) or run.bold
    document.add_paragraph().paragraph_format.space_after = Pt(1)
    return result


def render_card(document, card):
    section = document.add_section(WD_SECTION.NEW_PAGE)
    section_setup(section, landscape=True)
    section.top_margin = section.bottom_margin = Cm(1)
    section.left_margin = section.right_margin = Cm(1.5)
    heading = document.add_heading(card["heading"], level=1)
    heading.paragraph_format.space_before = Pt(0)
    heading.paragraph_format.space_after = Pt(4)
    for run in heading.runs:
        set_font(run, 12, bold=True, color=LAYOUT["heading_color"])
    title = document.add_paragraph(style="Title")
    title.paragraph_format.line_spacing = 1.1
    title.paragraph_format.space_after = Pt(4)
    set_font(title.add_run(card["title"]), size=18, bold=True)
    if card.get("project_name"):
        project = document.add_paragraph()
        project.paragraph_format.space_after = Pt(3)
        set_font(project.add_run(card["project_name"]), 9)
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(5)
    paragraph.paragraph_format.line_spacing = 1.1
    set_font(paragraph.add_run("阶段与范围  "), 10, bold=True)
    set_font(paragraph.add_run(card["stage"]), 10)
    grid = document.add_table(rows=2, cols=2)
    grid.autofit = False
    labels = [("delivered", "预期成果" if card["forecast"] else "代表成果"), ("method", "如何实现"),
              ("value", "有什么用"), ("next", "后续关注")]
    width = (LAYOUT["landscape_cm"][0] - 3) / 2
    for column in grid.columns:
        column.width = Cm(width)
    for row in grid.rows:
        row.height = Cm(3.1)
        row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    for cell, (key, label) in zip((cell for row in grid.rows for cell in row.cells), labels):
        cell.width = Cm(width)
        heading = cell.paragraphs[0]
        heading.paragraph_format.space_before = Pt(5)
        heading.paragraph_format.space_after = Pt(4)
        set_font(heading.add_run(label), 12, bold=True, color=LAYOUT["heading_color"])
        content = cell.add_paragraph()
        content.paragraph_format.line_spacing = 1.15
        content.paragraph_format.space_after = Pt(2)
        set_font(content.add_run(card[key]), 11)
    section_setup(document.add_section(WD_SECTION.NEW_PAGE))


def validate_docx(path, require_card=False):
    from lxml import etree

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(path) as package:
        if package.testzip():
            raise ValueError("Word文件完整性检查失败。")
        root = etree.fromstring(package.read("word/document.xml"))
    names = root.xpath("//w:bookmarkStart/@w:name", namespaces=namespace)
    targets = root.xpath("//w:hyperlink/@w:anchor", namespaces=namespace)
    if len(names) != len(set(names)) or any(target not in names for target in targets):
        raise ValueError("Word依据书签缺失或重复，停止交付。")
    landscape = root.xpath('//w:sectPr/w:pgSz[@w:orient="landscape"]', namespaces=namespace)
    if require_card and not landscape:
        raise ValueError("成果报告缺少横向卡片页。")
    if targets:
        raise ValueError("Word报告不应包含预览不支持的文内跳转。")
    return {"internal_links": len(targets), "bookmarks": len(names), "card": bool(landscape),
            "tables": len(root.xpath('//w:tbl', namespaces=namespace))}


def render_docx(markdown, path, title, card=None):
    markdown = without_navigation(markdown)
    document = Document()
    section_setup(document.sections[0])
    styles = [("Normal", LAYOUT["body_pt"]), ("Title", LAYOUT["title_pt"])]
    styles += [("Heading " + str(index), size) for index, size in enumerate(LAYOUT["heading_pt"], 1)]
    for name, size in styles:
        style = document.styles[name]
        style.font.name = TEXT_FONT
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string("000000" if name == "Title" else LAYOUT["heading_color"] if name.startswith("Heading") else LAYOUT["text_color"])
        style.paragraph_format.line_spacing = LAYOUT["line_spacing"]
        style.paragraph_format.space_after = Pt(7)
        for border in list(style._element.xpath('.//w:pBdr')):
            border.getparent().remove(border)
    for border in list(document.styles.element.xpath('.//w:pBdr')):
        border.getparent().remove(border)
    document.core_properties.title = title
    document.core_properties.author = "鲸研"
    footer = document.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_font(footer.add_run("第 "), 9)
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    set_font(footer.add_run(" 页"), 9)
    lines = markdown.splitlines()
    index = 0
    counter = [0]
    pending = []
    appendix = False
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        names = re.findall(r'<a\s+(?:[^>]*?\s)?(?:id|name)="([^"]+)"', line)
        if line.startswith("<a "):
            pending += list(dict.fromkeys(names))
            continue
        if line == CARD_START:
            if card is None:
                raise ValueError("卡片缺少已核对文字。")
            render_card(document, card)
            while index < len(lines) and lines[index].strip() != CARD_END:
                index += 1
            index += 1
            continue
        if line.startswith("|"):
            rows = [line]
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(lines[index].strip())
                index += 1
            parsed = [[part.strip() for part in re.split(r'(?<!\\)\|', row)[1:-1]] for row in rows]
            parsed = [row for row in parsed if not all(re.fullmatch(r':?-{3,}:?', cell) for cell in row)]
            if pending:
                target_paragraph = document.add_paragraph()
                target_paragraph.paragraph_format.keep_with_next = True
                target_paragraph.paragraph_format.space_before = Pt(0)
                target_paragraph.paragraph_format.space_after = Pt(0)
                target_paragraph.paragraph_format.line_spacing = Pt(1)
                add_bookmarks(target_paragraph, pending, counter)
                pending = []
            table(document, parsed, appendix, counter)
            continue
        heading = re.match(r'^(#{1,6})\s+(.+)', line)
        if heading:
            content = heading[2]
            if content.startswith("附页"):
                appendix = True
            paragraph = document.add_paragraph(style="Title" if len(heading[1]) == 1 else "Heading " + str(min(3, len(heading[1]) - 1)))
            color = "000000" if len(heading[1]) == 1 else LAYOUT["heading_color"] if len(heading[1]) == 2 else LAYOUT["subheading_color"]
            text_runs(paragraph, content, color=color)
        else:
            paragraph = document.add_paragraph()
            text_runs(paragraph, line[2:] if line.startswith(("- ", "> ")) else line)
            if re.fullmatch(r'\*\*[^*\n]{1,24}\*\*', line):
                paragraph.paragraph_format.keep_with_next = True
        if pending:
            add_bookmarks(paragraph, pending, counter)
            pending = []
    if pending:
        raise ValueError("Word依据目标缺少对应内容。")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
            **validate_docx(path, require_card=card is not None)}
