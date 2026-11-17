# 鲸研项目报告 Skill

可独立发布的 WorkBuddy Skill，支持飞书或本地取材，生成可编辑的项目履约审查、阶段成果分析，或仅任务书的预期成果分析。当前版本为 0.4.0。

## 使用

克隆仓库后，在 WorkBuddy 选择「从本地添加技能」，导入仓库下完整的 `project-report` 目录。如果已安装旧版本，先更新为这份目录。首次使用按 [使用说明](project-report/README_使用说明.md) 完成初始化和个人报告模型设置。首次初始化自动准备用户目录内的 Python 与锁定依赖，后续复用。飞书取材使用 WorkBuddy 中已连接账号的权限。

Windows 也可以从仓库根目录用 PowerShell 运行 `& .\project-report\scripts\run.ps1 init`，再用同一入口执行 `doctor` 查看环境状态。模型密钥按使用说明在本机设置，飞书连接器在 WorkBuddy 内授权。

```text
为我飞书「项目进展看板」里的全部项目生成履约审查和成果分析，并归档。
```

WorkBuddy 将范围和报告要求传给包内命令，由脚本清点、下载、生成、归档和回读。可从原批次继续处理未完成项目。命令见 [飞书取材与归档](project-report/references/feishu-base.md)，首次配置见 [运行与初始化](project-report/references/report-runtime.md)。

## 包内结构

- `project-report/SKILL.md` 为宿主入口，`references/` 保存业务流程和阅读规范。
- `runtime/scripts/` 保存文档解析、OCR、模型调用、提示词及来源和一致性核对。
- `runtime/config/` 保存通用规则、合成校准示例和成果表达方法。
- `templates/document-layout.json` 控制 Word 的字号、页面和表格样式。
- `scripts/` 包含初始化、个人密钥设置、飞书批次执行和 Word 导出。
- `requirements.lock` 固定依赖。环境、真实材料、个人密钥和运行底稿分别存入用户工作目录。

正式附件使用独立 DOCX。成果报告含事实表、简版、科普长版和可编辑横版卡片。报告附页以普通文字列出关键内容的原材料位置，不依赖文内跳转；完整原文和核对记录保存在本地底稿。材料映射明确时直接执行，表格重名、多版本或需要新建归档列时再简短询问。

文档以深蓝章节标题、浅蓝表头和少量关键句加粗区分阅读层次。卡片保留原生可编辑文字，不使用生图调用，不增加模型请求。当前优先验证宿主兼容性和生成稳定性。

## 构建与验证

在仓库根目录运行：

```bash
python3 scripts/build_skill_package.py
```

打包入口按文件类型和目录清单生成 `dist/project-report.zip`，可直接在 WorkBuddy 导入。首次安装需要下载依赖。

开发检查包含缺材料分流、个人凭据隔离、无跳转的来源位置、横向卡片、独立解压运行，以及真实项目的原文和一致性检查。WorkBuddy 的客户端取材与归档验收另行执行。
