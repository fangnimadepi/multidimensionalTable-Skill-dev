---
name: project-report
display_name: 鲸研项目报告
display_name_en: Research Project Reports
description: 根据项目任务书和阶段总结报告生成履约审查、阶段成果与后续推进分析，或根据任务书生成预期成果分析。支持本地材料和飞书多维表格取材及归档。
description_zh: 读项目材料、生成履约审查和成果分析，并将飞书项目的报告归档到原记录。
description_en: Review research project progress and explain outcomes using project documents, with optional Feishu Base archiving.
version: 0.4.2
author: fangnimadepi
examples_zh:
  - 根据附件中的任务书和中期总结报告，生成履约审查和成果分析。
  - 这个项目只有任务书，帮我生成预期成果分析。
  - 从这张飞书表读取指定项目的材料，先出报告草稿给我看。
  - 给飞书「项目进展看板」里的指定项目生成总结报告。
---

# 鲸研项目报告

**飞书项目的默认交付。** 用户用鲸研指定飞书多维表格及项目范围，要求「生成报告」「生成总结报告」或「做项目分析」时，执行标准流程。材料齐全时生成履约审查和成果分析两份 Word，并归档到原项目记录。用户明确说「只做审查」「只做成果分析」或「任务书版」时，分别指定 `review`、`achievement` 或 `forecast`。用户明确只要本地文稿、先预览或暂不写回时，保留本地结果。只读查询和清点也保留只读行为。

**执行飞书任务。** 按 [飞书取材与归档](references/feishu-base.md) 运行包内 `feishu inspect`。标准流程传 `--archive`，使用默认的两种报告。核对清点结果里的项目、报告类型和 `archive`，再用原批次运行 `feishu run`。脚本负责下载、出稿、归档、回读和续跑。缺少归档列、材料歧义或权限不足时反馈对应步骤。要求更新同类旧报告才传 `--replace`。只使用已连接的飞书用户身份。

缺阶段总结时，成果流程生成预期成果分析并说明履约审查缺材料。已有阶段附件读取失败时，报告实际失败步骤。

**本地材料和首次配置。** macOS/Linux 使用 `scripts/run.sh`，Windows 使用 `scripts/run.ps1`。入口会复用已有个人环境。按 [运行与初始化](references/report-runtime.md) 执行 `generate` 或检查读取结果。配置密钥或更换报告模型时读 [个人模型配置](references/model-setup.md)。任务书用 DOCX，阶段总结用 DOCX 或 PDF。原始材料仅作为分析依据。

**手动配置报告模型。** 在当前技能目录将 `config/model.example.json` 复制为 `config/model.local.json`，用户在本机填写 API 地址、模型名和密钥。用 `init` 返回的 `manual_model_config` 定位实际文件，用 `check-model` 验证连接。仅由脚本读取密钥。没有手动配置文件时可继续使用 `configure`。

**交付。** 以 `checked.json` 核对成稿，以 `archive.json` 和回读状态确认归档。涉及归档的项目到 `archived` 才算交付完成。批量任务简报报告类型、归档完成、缺材料及失败情况。正文和附页用可搜索的文字列明来源位置。完整引文和核对记录留在本机底稿。报告结构见 [履约审查](references/review-report.md) 和 [成果分析](references/achievement-reports.md)。
