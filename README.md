# 鲸研项目报告 Skill

可独立发布的 WorkBuddy Skill，支持飞书或本地取材，生成可编辑的项目履约审查、阶段成果分析，或仅任务书的预期成果分析。当前版本为 0.5.2。

## 使用

克隆仓库后，在 WorkBuddy 选择「从本地添加技能」，导入完整的 project-report 目录。默认由 WorkBuddy 当前会话模型撰写与自查，内置积分模型和自定义模型使用同一流程，不需要额外配置报告密钥。原有独立 API 配置保留，只有明确传 --mode api 时读取。首次只准备 Word/文字 PDF 基础依赖，扫描 PDF 按需准备 OCR。飞书取材沿用已连接账号的权限。

Windows 可从仓库根目录执行 & .\project-report\scripts\run.ps1 doctor 只读检查，尚未初始化时再执行 init。完成后选中技能，用一句话发起任务即可。具体见 [使用说明](project-report/README_使用说明.md)。

```text
为我飞书「项目进展看板」里的全部项目生成总结报告。
```

飞书项目的总结报告走 `feishu deliver`。脚本清点并下载材料，按事实提取、两稿撰写、来源自查向当前会话返回固定任务，再统一导出、归档并回读确认。needs_model 时读取可读任务及响应骨架，填写后执行固定 next_command；统一入口完成校验、提交及续跑，无需宿主写脚本。引用只选来源编号，原文由程序回填；材料保留完整底稿，模型输入减少行政信息和重复元数据。指定「先预览，暂不写回」时可仅生成本地文稿。命令和 Windows 入口见 [飞书取材与归档](project-report/references/feishu-base.md)，首次配置见 [运行与初始化](project-report/references/report-runtime.md)。

## 包内结构

- `project-report/SKILL.md` 为宿主入口，`references/` 保存业务流程和阅读规范。
- `project-report/config/model.example.json` 提供手动模型配置模板，个人配置保存在同目录的 `model.local.json`。
- `runtime/scripts/` 保存文档解析、OCR、模型调用、提示词及来源和一致性核对。
- `runtime/config/` 保存通用规则、合成校准示例和成果表达方法。
- `templates/document-layout.json` 控制 Word 的字号、页面和表格样式。
- `scripts/host_*.py` 提供不依赖模型密钥的宿主阶段协议、材料单元、报告结构检查与导出。
- `scripts/` 同时保留初始化、可选独立 API 设置、飞书批次执行和 Word 导出。
- `requirements.lock` 固定基础依赖，`requirements-ocr.lock` 固定可选 OCR 扩展。环境、真实材料和运行底稿保存在用户工作目录。个人密钥由本机配置读取。

正式附件使用独立 DOCX。成果报告含事实表、简版、科普长版和可编辑横版卡片。报告附页以普通文字列出关键内容的原材料位置，不依赖文内跳转；完整原文和核对记录保存在本地底稿。材料映射明确时直接执行，表格重名、多版本或需要新建归档列时再简短询问。

文档以深蓝章节标题、浅蓝表头和少量关键句加粗区分阅读层次。卡片保留原生可编辑文字，不使用生图调用，不增加模型请求。当前优先验证宿主兼容性和生成稳定性。

## 构建与验证

在仓库根目录运行：

```bash
python3 scripts/build_skill_package.py
```

打包入口按文件类型和目录清单生成 `dist/project-report.zip`，可直接在 WorkBuddy 导入。首次安装需要下载依赖。

开发检查包含缺材料分流、个人凭据隔离、无跳转的来源位置、横向卡片、独立解压运行，以及宿主模式的阶段恢复、两稿齐全、缺材料分流、模拟飞书归档和独立 API 回归。WorkBuddy 的客户端取材与归档验收另行执行。

默认宿主协议详见 [host-workflow.md](project-report/references/host-workflow.md)。程序检查原文、数字与结构，语义由宿主自查；独立 API 链路继续保留原有模型复核。模拟宿主测试不等同于 WorkBuddy 客户端实测，内置/自定义模型需分别验收。
