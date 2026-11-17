# 独立运行与初始化

报告核心、提示词、规则、合成校准示例及文档模板都在技能包中。`runtime/` 保存报告核心，`templates/document-layout.json` 控制 Word 排版。初始化后，把同版本核心校验复制到个人工作区的 `_runtime/`，生成记录也在该处保存。

## 首次初始化

以下路径相对完整 Skill 目录，执行时使用实际解压位置。macOS/Linux 使用系统 shell，Windows 使用 PowerShell。

```bash
sh scripts/run.sh init
```

```powershell
& .\scripts\run.ps1 init
```

初始化优先查找可用 Python 3.11—3.13，以及 WorkBuddy 已管理的 Python。未找到时从 uv 官方渠道初始化用户目录内的 uv 与 Python 3.12，随后按包内 `requirements.lock` 安装解析、OCR 和 Word 依赖。环境默认保存在用户主目录的 `.jingyan/`。首次安装需要访问依赖下载站点，后续按锁定版本复用。网络、权限或组织策略失败时报告具体失败步骤，不修改安全策略。

用 `JINGYAN_HOME` 指定其他个人数据目录，用 `JINGYAN_CONFIG` 指定已有个人配置文件。初始化回执中的 `python` 和 `config` 可供宿主后续直接调用，默认入口也可继续使用。示例配置见 [workspace-config.example.json](workspace-config.example.json)。个人配置包含工作目录、模型和文件限制，自动安装的解释器由入口定位。

入口会探测环境能否真正启动，损坏环境仅将 `venv` 子目录保留为 `venv-incomplete-*` 后重建，不删除个人数据根目录、密钥、配置或底稿。macOS/Linux 使用解释器符号链接，兼容 WorkBuddy 托管 Python 的共享库布局。已有配置通过 `JINGYAN_CONFIG` 指定时，回执返回该文件及实际密钥配置状态。

读取命令的实际退出码及 JSON 状态。需要缩短展示时先保存完整输出并保留命令退出码，再读取日志尾部；不要将生成或初始化命令直接接入 `tail`、`head` 后用末端状态判成功。初始化失败后记录具体步骤，不能手动删除 `~/.jingyan` 或任意更换、安装工具来绕过入口。

## 配置模型并检查

对飞书表格使用 [飞书批次入口](feishu-base.md)。下面的 `generate` 用于已经准备好的本地材料。

按 [个人模型配置](model-setup.md) 使用技能目录下的手动配置文件，或沿用 WorkBuddy 对话配置。`doctor` 检查本机状态，`check-model` 向当前接口发送一条验证请求。macOS/Linux 示例中 `$ENTRY` 为 `scripts/run.sh` 的绝对路径。

```bash
sh "$ENTRY" doctor
sh "$ENTRY" check-model
sh "$ENTRY" generate --run-dir "$RUN" \
  --taskbook 'raw/taskbook/项目任务书.docx' --report 'raw/report/阶段总结.pdf' \
  --types review achievement --record-id "$RECORD" --project-name "$PROJECT"
```

Windows 使用 `& .\scripts\run.ps1` 及相同参数。只要一份报告时用 `--types review` 或 `--types achievement`。只有任务书时使用 `--types forecast`，省略 `--report`。`read --run-dir ... --role report --input ...` 用于检查 PDF 页码及 OCR 情况，正常生成会自行解析。

## 回执与恢复

成功返回 `status: checked`、原材料摘要、检查结果和 `documents`。每个文档项使用 `docx` 字段给出实际文件路径，另有 SHA-256、字节大小和卡片状态；为兼容已有回执保留的 `internal_links`、`bookmarks` 均为 0。`checked.json`、可编辑 Markdown 及 DOCX 保存在项目 `reports/` 下。正式归档上传回执对应的 DOCX。内部完整正文、原文上下文和审核记录保留在 `diagnostics` 指向的本地底稿，精简附页不改变核对标准。

重试复用相同项目目录、材料和模型设置，已完成步骤按摘要核验后续跑。不同版本的核心分别保存，避免把旧版本审批结果冒充本轮检查。

正式报告不使用文内跳转，附页通过内容名称和原材料位置供读者查找。底稿中的原有链接和引用绑定继续用于程序检查，不复制为正式报告中的跳转按钮。正文、表格与卡片文字可编辑；编辑后的文件属于人工修改稿，原有程序回执对应生成时的原文件。

正文样式由 `templates/document-layout.json` 控制。章节标题采用深蓝，表头浅蓝，关键完整句适当加粗。加粗只包裹已有文字，不改动数字、条件和判断。横版卡片继续使用可编辑文字，不调用图片生成。外观更新时可复用已检查底稿重新导出，无需为排版重新生成正文。

只修正排版时使用 `reexport`。传入本轮已经检查的回执及相同原材料，不调用模型或 OCR，生成新的导出目录与回执。原回执保留。材料摘要、底稿摘要或核心版本不一致时停止，需重新生成。

```bash
sh "$ENTRY" reexport --run-dir "$RUN" --receipt "$CHECKED_RECEIPT" \
  --taskbook 'raw/taskbook/项目任务书.docx' --report 'raw/report/阶段总结.pdf'
```
