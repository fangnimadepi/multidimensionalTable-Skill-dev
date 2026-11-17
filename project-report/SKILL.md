---
name: project-report
display_name: 鲸研项目报告
display_name_en: Research Project Reports
description: 根据项目任务书和阶段总结报告生成履约审查、阶段成果与后续推进分析，或根据任务书生成预期成果分析。支持本地材料和飞书多维表格取材及归档。
description_zh: 读项目材料、核对计划与进展、撰写成果报告，并按需归档到飞书。
description_en: Review research project progress and explain outcomes using project documents, with optional Feishu Base archiving.
version: 0.4.1
author: fangnimadepi
examples_zh:
  - 根据附件中的任务书和中期总结报告，生成履约审查和成果分析。
  - 这个项目只有任务书，帮我生成预期成果分析。
  - 从这张飞书表读取指定项目的材料，先出报告草稿给我看。
  - 为飞书「项目进展看板」里的全部项目生成履约审查和成果分析，并归档。
---

# 鲸研项目报告

用户只需说明表格、项目范围和报告需求。「审查」对应 `review`，「成果报告」对应 `achievement`，「任务书版」对应 `forecast`。用户只要求成果或同时要求两份报告而缺阶段总结时，生成预期成果分析。已有阶段附件读取失败时，报告实际失败步骤。

**飞书任务优先执行包内 `feishu` 命令。** 读 [飞书取材与归档](references/feishu-base.md)，把本次范围和报告类型传给 `feishu inspect`。清点成功后用简短一句说明识别结果，再直接运行 `feishu run`。脚本负责下载、出稿、归档、回读及续跑。常规任务无需另外阅读连接器接口帮助，也无需临时编写业务脚本。匹配存在歧义时只请用户补充范围、链接或材料选择。用户授权归档才传 `--archive`，要求更新同类旧报告才传 `--replace`。只使用已连接的飞书用户身份。

**本地材料和首次配置。** macOS/Linux 使用 `scripts/run.sh`，Windows 使用 `scripts/run.ps1`。入口会复用已有个人环境。按 [运行与初始化](references/report-runtime.md) 执行 `generate` 或检查读取结果。配置密钥或更换报告模型时读 [个人模型配置](references/model-setup.md)。任务书用 DOCX，阶段总结用 DOCX 或 PDF。原始材料仅作为分析依据。

**手动配置报告模型。** 在当前技能目录将 `config/model.example.json` 复制为 `config/model.local.json`，用户在本机填写 API 地址、模型名和密钥。用 `init` 返回的 `manual_model_config` 定位实际文件，用 `check-model` 验证连接。仅由脚本读取密钥。没有手动配置文件时可继续使用 `configure`。

**交付。** 以 `checked.json` 核对已生成文件，以归档回执确认已写回。批量任务简报完成、任务书版、缺材料及失败项目。正文和附页用可搜索的文字列明来源位置。完整引文和核对记录留在本机底稿。成果报告提供可编辑的横版文字卡片。报告结构见 [履约审查](references/review-report.md) 和 [成果分析](references/achievement-reports.md)。
