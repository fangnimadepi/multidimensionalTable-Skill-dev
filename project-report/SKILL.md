---
name: project-report
display_name: 鲸研项目报告
display_name_en: Research Project Reports
description: 根据项目任务书和阶段总结报告生成履约审查、阶段成果与后续推进分析，或根据任务书生成预期成果分析。支持本地材料和飞书多维表格取材及归档。
description_zh: 读项目材料、生成履约审查和成果分析，并将飞书项目的报告归档到原记录。
description_en: Review project progress, explain outcomes, and archive Feishu project reports to the original record.
version: 0.4.5
author: fangnimadepi
examples_zh:
  - 根据附件中的任务书和中期总结报告，生成履约审查和成果分析。
  - 这个项目只有任务书，帮我生成预期成果分析。
  - 从这张飞书表读取指定项目的材料，先出报告草稿给我看。
  - 给飞书「项目进展看板」里的指定项目生成总结报告。
---

# 鲸研项目报告

**飞书项目的默认交付。** 用户用鲸研指定飞书多维表格及项目范围，要求「生成报告」「生成总结报告」或「做项目分析」时，按 [飞书取材与归档](references/feishu-base.md) 直接执行 `feishu deliver`。该入口固定生成履约审查和成果分析两份 Word，写回原项目记录并回读核对。Windows 已完成初始化时，直接用已就绪的 Python 执行 `feishu_batch.py deliver`。

**单种报告与预览。** 用户明确说「只做审查」「只做成果分析」或「任务书版」时，使用 `inspect --archive --types` 并分别指定 `review`、`achievement` 或 `forecast`。明确要求先预览或只交本地文稿时用 `--local-only`。`inspect` 只清点，用户只要求清点时到此结束。执行已清点计划或续跑用 `run --batch`，交付方式取自计划。要求更新同类旧报告才传 `--replace`。只使用已连接的飞书用户身份。

缺阶段总结时，成果流程生成预期成果分析并说明履约审查缺材料。缺少归档列、材料歧义、权限不足或附件读取失败时，报告对应步骤。

**本地材料和首次配置。** macOS/Linux 使用 `scripts/run.sh`，Windows 使用 `scripts/run.ps1`。入口会复用已有个人环境。按 [运行与初始化](references/report-runtime.md) 执行 `generate` 或检查读取结果。配置密钥或更换报告模型时读 [个人模型配置](references/model-setup.md)。任务书用 DOCX，阶段总结用 DOCX 或 PDF。原始材料仅作为分析依据。

**手动配置报告模型。** 在当前技能目录将 `config/model.example.json` 复制为 `config/model.local.json`，用户在本机填写 API 地址、模型名和密钥。用 `init` 返回的 `manual_model_config` 定位实际文件，用 `check-model` 验证连接。仅由脚本读取密钥。没有手动配置文件时可继续使用 `configure`。

**交付。** 以 `checked.json` 核对成稿，以 `archive.json` 和回读状态确认归档。涉及归档的项目到 `archived` 才算交付完成。直接根据批次结果汇报报告类型、归档状态、缺材料及失败情况。完整引文和核对记录留在本机底稿。报告结构见 [履约审查](references/review-report.md) 和 [成果分析](references/achievement-reports.md)。

**失败时的处理。** 先读命令返回的 `failure.error_code`、`retryable`、`next_action` 和诊断目录。`retryable: false` 时停止该项目，将错误码、批次和诊断目录反馈给用户，并继续批次中的其他项目。临时连接失败可从原批次重试一次，再次失败即反馈。源码分析、清除步骤缓存和更改校验规则交给维护人员处理。保留已下载材料和完成步骤。`config/model.local.json`、密钥文件及 `.env` 只由配置与检查脚本读取，宿主工具不得直接读取或打印其中内容。检查模型连接用 `check-model`。

报告检查失败时，按 `validation_stage` 说明具体生成环节。`validation_file` 是该次检查反馈的完整路径，可只读其中的 `feedback` 或 `errors` 来解释拒绝原因。附件损坏、内容缺失或解析失败的判断须有对应下载或解析错误支持。
