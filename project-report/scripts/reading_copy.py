"""Produce concise reader copies while retaining the engine's full evidence files."""

import re

CARD_START = "<!-- jingyan-card-start -->"
CARD_END = "<!-- jingyan-card-end -->"


def emphasize_lead(text):
    """Emphasize one intact sentence without rewording its qualifications."""
    if "**" in text or "\n" in text:
        return text
    ending = re.search(r'[。！？][”’」』]?', text)
    if ending and 12 <= ending.end() <= 160:
        return "**" + text[:ending.end()] + "**" + text[ending.end():]
    return text


def emphasize_review_summary(markdown):
    lines = markdown.splitlines()
    remaining = 0
    for index, line in enumerate(lines):
        if line.startswith("#"):
            remaining = 2 if re.fullmatch(r'#{2,4}\s+综合审查意见', line.strip()) else 0
        elif remaining and line.strip() and not line.startswith(("<", "[", "|")):
            lines[index] = emphasize_lead(line)
            remaining -= 1
    return "\n".join(lines) + "\n"


def without_navigation(markdown):
    """Keep source labels and locations readable without preview-only links."""
    markdown = re.sub(r'<a\s+[^>]*(?:id|name)="[^"]+"[^>]*>\s*</a>', '', markdown)

    def label(match):
        text = match[1]
        return '' if re.fullmatch(r'查看.*(?:依据|来源)|返回正文', text) else text

    markdown = re.sub(r'\[([^\]\n]+)\]\(#[^)\s]+\)', label, markdown)
    return re.sub(r'\n{3,}', '\n\n', markdown).strip() + '\n'


def compact_locations(records):
    grouped = {}
    for record in records:
        side = "任务书" if record["side"] == "plan" else "总结报告"
        location = record["location"]
        group = grouped.setdefault(side, {"segments": {}, "other": []})
        paragraph = re.fullmatch(r'(?:[PR]-)?P(\d+)', location)
        row = re.fullmatch(r'(?:[PR]-)?T(\d+)-R(\d+)', location)
        page = re.fullmatch(r'(?:[PR]-)?PDF-P(\d+)', location)
        if paragraph:
            key, number = ("正文第", "段"), int(paragraph[1])
        elif row:
            key, number = ("第" + row[1] + "表第", "行"), int(row[2])
        elif page:
            key, number = ("原PDF第", "页"), int(page[1])
        else:
            if location not in group["other"]:
                group["other"].append(location)
            continue
        group["segments"].setdefault(key, set()).add(number)
    output = []
    for side, group in grouped.items():
        locations = []
        for (prefix, suffix), numbers in group["segments"].items():
            spans = []
            for number in sorted(numbers):
                if spans and number == spans[-1][-1] + 1:
                    spans[-1].append(number)
                else:
                    spans.append([number])
            locations.append(prefix + "、".join(str(s[0]) if len(s) == 1 else str(s[0]) + "—" + str(s[-1]) for s in spans) + suffix)
        locations += group["other"]
        output.append(side + "：" + "；".join(locations[:4]) + ("等相关位置" if len(locations) > 4 else ""))
    return "<br>".join(output)


def review_appendix(report):
    sources = {(source["ref"], source["quote"]): source for source in report["source_index"]}

    def locations(values):
        selected = []
        for value in values:
            for cite in value.get("citations", []):
                source = sources.get((cite["ref"], cite["quote"]))
                if source is None:
                    raise ValueError("关键依据缺少原文绑定，停止导出。")
                selected.append(source)
        return compact_locations(selected)

    lines = ["## 材料来源与重点定位", ""]
    conclusions = [item for item in report["overall_assessment"]["statements"] if item["section"] == "conclusion"]
    rows = {"综合意见与主要成果": locations(conclusions)}
    groups = report.get("presentation", {}).get("categorized_findings", {}).get("items", [])
    import project_report_findings as findings

    grouped = {}
    for topic in sorted(groups, key=lambda topic: findings.CATEGORIES.index(topic["category"])):
        grouped.setdefault(topic["category"], []).extend(topic[key] for key in ("plan_requirement", "reported_progress", "difference"))
    for category, fields in grouped.items():
        rows[category] = locations(fields)
    questions = report.get("presentation", {}).get("pi_confirmation", {}).get("items", [])
    if questions:
        rows["确认事项的材料位置"] = locations(questions)
    lines += ["- **" + label + "：** " + position.replace("<br>", "；") for label, position in rows.items() if position]
    return "\n".join(lines) + "\n"


def achievement_copy(report, markdown):
    from achievement_expression import QUESTIONS
    from project_report_contract import cell

    draft = report["draft"]
    if not draft.get("card"):
        raise ValueError("本次成果报告缺少已核对的横版卡片，请重新生成该报告。")
    cover = markdown.split("## 一、", 1)[0]
    lines = [cover.rstrip(), "", "## 一、事实成果提炼", "", "| 核心问题 | 事实提炼 |", "| --- | --- |"]
    lines += ["| " + label + " | " + cell(emphasize_lead(draft["fact_sheet"][key]["text"]) if key in {"increment", "value"}
                                          else draft["fact_sheet"][key]["text"]) + " |" for key, label in QUESTIONS.items()]
    lines += ["", "## 二、简版", "", "### " + draft["value_title"], ""]
    emphasized = 0
    for paragraph in draft["short_version"]:
        text = paragraph["text"]
        if emphasized < 2 and paragraph["kind"] in {"事实归纳", "价值分析"}:
            text = emphasize_lead(text)
            emphasized += text != paragraph["text"]
        lines += [text, ""]
    lines += ["## 三、科普长版", ""]
    for paragraph in draft.get("long_intro", []):
        lines += [paragraph["text"], ""]
    for section in draft["long_sections"]:
        lines += ["### " + section["title"], ""]
        for index, paragraph in enumerate(section["paragraphs"]):
            text = paragraph["text"]
            if index == 0 and paragraph["kind"] in {"事实归纳", "价值分析"}:
                text = emphasize_lead(text)
            lines += [text, ""]
    card = {key: value["text"] for key, value in draft["card"].items()}
    card.update(title=draft["value_title"], project_name=report["source"]["project_name"], heading="四 横版展示卡片", forecast=False)
    lines += card_markdown(card)
    appendix = ["## 材料来源与重点定位", ""]
    if draft.get("questions"):
        for question in draft["questions"]:
            appendix += [question["text"], ""]
    from achievement_expression import statements

    used = {key for statement in statements(draft) for key in statement["fact_ids"]}
    categories = {}
    for fact in report["facts"]:
        if fact["fact_id"] in used:
            categories.setdefault(fact["category"], []).extend(report["source"]["records"][cite["ref"]] for cite in fact["citations"])
    appendix += ["- **" + category + "：** " + compact_locations(records).replace("<br>", "；") for category, records in categories.items()]
    return "\n".join(lines), "\n".join(appendix) + "\n", card


def card_markdown(card):
    labels = {"delivered": "预期成果" if card["forecast"] else "代表成果", "method": "如何实现", "value": "有什么用",
              "stage": "当前口径", "next": "后续关注"}
    lines = [CARD_START, "## " + card["heading"], "", "### " + card["title"], ""]
    lines += ["**" + label + "** " + card[key] + "\n" for key, label in labels.items()]
    return lines + [CARD_END, ""]


def forecast_copy(markdown):
    import html

    body, appendix = markdown.split("## 附录：", 1)
    body = re.sub(r'（\[依据\d+\]\(#[^)]+\)(?:、\[依据\d+\]\(#[^)]+\))*）', '', body)
    body = body.split("### 后续验证", 1)[0].rstrip() + "\n"
    # These are exact, already-checked taskbook paragraphs, without new model claims.
    brief = body.split("### 简版", 1)[1].split("### 科普长版", 1)[0].strip()
    title = re.search(r'\*\*([^\n]+)\*\*', brief)
    overview = body.split("### 项目概述", 1)[1].split("### 简版", 1)[0].strip()
    target_table = body.split("## 一、目标成果提炼", 1)[1].split("## 二、", 1)[0]
    outcomes = [line for line in target_table.splitlines() if line.startswith("| ")
                and not re.match(r'^\|\s*(?:---|预期成果)', line)]
    # The card names the deliverables. Detailed metrics remain in the checked
    # target table instead of overflowing a slide with its entire cells.
    delivered = "；".join(html.unescape(line.split("|")[1].strip()) for line in outcomes[:3])
    if not delivered:
        delivered = overview
    card = {"heading": "三 横版展示卡片", "title": html.unescape(title[1]) if title else "项目预期成果",
            "project_name": "", "delivered": delivered, "method": html.unescape(overview),
            "value": html.unescape(re.sub(r'^\*\*[^\n]+\*\*\s*', '', brief)),
            "stage": "任务书版 预期成果", "next": "结合后续阶段总结、测试或演示材料核对实际交付与关键指标。", "forecast": True}
    body += "\n" + "\n".join(card_markdown(card))
    result = ["## 材料来源与重点定位", ""]
    used = set()
    themes = {}
    for section in re.split(r'(?=<a id="forecast-source-)', appendix):
        match = re.search(r'<a id="([^"]+)"', section)
        if not match:
            continue
        heading = re.search(r'### 依据\d+｜([^\n]+)', section)
        location = re.search(r'^位置 ([^\n]+)', section, re.M)
        if not heading or not location:
            raise ValueError("任务书版依据缺少章节或位置。")
        key = (heading[1], location[1])
        if key not in used:
            themes.setdefault(heading[1], []).append(location[1])
            used.add(key)
    result += ["- **" + heading + "：** " + "；".join(positions) for heading, positions in themes.items()]
    return body, "\n".join(result) + "\n", card


def compose(kind, markdown, report=None):
    if kind == "review":
        body = emphasize_review_summary(markdown.split("## 附录：", 1)[0])
        result = body, review_appendix(report), None
    elif kind == "achievement":
        result = achievement_copy(report, markdown)
    elif kind == "forecast":
        result = forecast_copy(markdown)
    else:
        raise ValueError("未知报告类型。")
    body, appendix, card = result
    return without_navigation(body), without_navigation(appendix), card
