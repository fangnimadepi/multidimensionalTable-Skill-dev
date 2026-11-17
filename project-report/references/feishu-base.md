# 飞书多维表格取材与归档

使用 WorkBuddy 中已连接的飞书账号。标准任务直接用 `deliver`。命令从当前 WorkBuddy 任务目录执行，附件暂存使用连接器有权访问的任务目录。

```bash
sh "$ENTRY" feishu deliver --target '飞书表格链接或确切名称' --project '确切项目名'
```

`$ENTRY` 指当前 Skill 的 `scripts/run.sh`。Windows 完成初始化后，直接用已有虚拟环境和实际安装的 Skill，避免重复初始化。以下 `$Skill` 指已加载的 Skill 根目录。

```powershell
$JingyanDir = if ($env:JINGYAN_HOME) { $env:JINGYAN_HOME } else { Join-Path $env:USERPROFILE '.jingyan' }
$Py = Join-Path $JingyanDir 'venv\Scripts\python.exe'
$Config = if ($env:JINGYAN_CONFIG) { $env:JINGYAN_CONFIG } else { Join-Path $JingyanDir '鲸研配置.json' }
$Entry = Join-Path $Skill 'scripts\feishu_batch.py'
& $Py $Entry --config $Config deliver --target '飞书表格链接或确切名称' --project '确切项目名'
```

直接调用 `feishu_batch.py` 时，子命令为 `deliver`、`inspect` 或 `run`。更新后可用 `& $Py $Entry --version` 确认已加载目录的版本。处理状态以退出码和 JSON 为准。`needs_selection` 需要补信息，`incomplete` 查看逐项目结果和剩余范围。批次结果含实际 Skill 版本、清点时长和各项目的下载、生成及归档时长。

- **表格与范围**。按链接或准确表名定位。名称查不到、结果不唯一或搜索缺权限时，直接请用户提供链接。多个子表用 `--table` 指定。使用视图链接时按该视图处理，用户明确要求全表时加 `--whole-table`。`--all` 表示当前范围的全部项目，`--count N` 仅在非空项目恰好为 N 时成立。用户指定前 N 项时，需先确定视图和排序，再用 `--first N`。指定项目时重复使用 `--project '项目名'`。用户用 test02、test03 指代「test02:项目名」等记录时，可直接传简称，脚本只接受唯一的冒号前缀匹配，歧义时返回候选供选择。
- **材料**。脚本识别明确的项目名、任务书和阶段总结列。唯一文本列叫「文本」时可用作项目名，有多个候选时通过 `--name-column` 指定。材料或归档列识别困难时使用对应的 `--taskbook-column`、`--report-column` 或 `--output-column`。确认整张表没有阶段总结时可用 `--no-report`。选定版本后，可用 `--material RECORD ROLE ATTACHMENT` 指定清点结果中对应的记录标识、`taskbook` 或 `report` 角色和附件标识。将技术标识留给宿主，用户只需选择文件名。可先处理其他已明确的项目。
- **单种报告和预览**。使用相同的范围参数，执行 `inspect --archive --types review`、`inspect --archive --types achievement` 或 `inspect --archive --types forecast`。仅本地出稿用 `inspect --local-only`，可加 `--types`。`inspect` 需要明确交付方式，旧的省略交付方式写法会报参数错误。清点成功后用 `run --batch '清点返回的批次绝对路径'` 执行。
- **归档位置**。默认追加到原项目记录的归档列。缺少归档列时集中确认新建或指定已有附件列，之后重新执行标准命令或原清点命令。列建好之前保留归档待办。明确更新旧报告时传 `--replace`，新文件摘要通过回读后才移除同类型、归属有回执的旧报告。认不出来源的旧附件仍保留。
- **续跑**。`deliver` 清点后输出批次路径。用 `run --batch` 从同一文件继续，保留清点时选定的类型和归档方式。将历史本地批次改为两份报告并归档时，启动新的 `deliver`。宿主单次命令时限较短时，可用 `--limit 1` 每次执行一个项目。生成失败先读 `failure`。`retryable: true` 时可用原批次 `--retry-failed` 重试一次，`false` 时停止该项目并交付错误码与诊断路径。维护人员修复并更新运行核心后，可从原批次继续。归档失败重试时先读目标格，再补写缺少的附件。材料发生变化时重新清点受影响项目。

汇报项目数、生成的报告类型、归档状态和材料缺项。`archived` 表示已经回读确认。`generated` 表示本地出稿。`archive_failed` 时交付本地成稿，并说明写回失败的具体原因及续跑方式。各项目的 `checked.json` 是成稿回执，`archive.json` 记录归档验证。参见 [批量处理](batch-processing.md) 和 [材料位置](material-handling.md)。
