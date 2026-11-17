# 飞书多维表格取材与归档

使用 WorkBuddy 中已连接的飞书账号。一个业务请求通常执行 `inspect` 和 `run` 两次包内命令。命令应从当前 WorkBuddy 任务目录执行，附件暂存会使用连接器有权访问的任务目录。首轮连接器可用性检查由脚本完成。

```bash
sh "$ENTRY" feishu inspect --target '飞书表格链接或确切名称' --count 3 --types review achievement --archive
sh "$ENTRY" feishu run --batch '清点返回的批次绝对路径'
```

`$ENTRY` 指当前 Skill 的 `scripts/run.sh`。Windows 使用 `& 'Skill 的实际目录\scripts\run.ps1'`，参数相同。入口从 `JINGYAN_CONFIG` 或个人配置取得工作目录和报告模型。使用实际退出码和 JSON 状态。`needs_selection` 需要补信息，`incomplete` 查看逐项目结果和剩余范围。

- **表格与范围**。按链接或准确表名定位。名称查不到、结果不唯一或搜索缺权限时，直接请用户提供链接。多个子表用 `--table` 指定。使用视图链接时按该视图处理，用户明确要求全表时加 `--whole-table`。`--all` 表示当前范围的全部项目，`--count N` 仅在非空项目恰好为 N 时成立。用户指定前 N 项时，需先确定视图和排序，再用 `--first N`。指定项目时重复使用 `--project '确切项目名'`。
- **材料**。脚本识别明确的项目名、任务书和阶段总结列。识别困难时，用 `--name-column`、`--taskbook-column`、`--report-column` 或 `--output-column` 指定对应列名。确认整张表没有阶段总结时可用 `--no-report`。选定版本后，可用 `--material RECORD ROLE ATTACHMENT` 指定清点结果中对应的记录标识、`taskbook` 或 `report` 角色和附件标识。将技术标识留给宿主，用户只需选择文件名。可先处理其他已明确的项目。
- **归档授权**。仅用户要求写回时传 `--archive`。默认追加到已有归档列。缺少归档列或使用特殊归档结构时，由宿主按用户已经指定的列处理，或集中确认一次，必要时引用 `lark-base` 的能力。明确更新旧报告时同时传 `--replace`，新文件摘要通过回读后才移除同类型、归属有回执的旧报告。认不出来源的旧附件仍保留。
- **续跑**。使用同一个 `--batch` 路径继续。给宿主命令留足报告生成时间。宿主单次命令时限较短时，可用 `--limit 1` 每次执行一个项目，继续同一清单即可。生成失败需要重试时用 `--retry-failed`，复用原始材料和已完成检查。归档失败重试时先读目标格，再补写缺少的附件。如果材料发生变化，应重新清点受影响的项目。

汇报项目数、材料缺项和最终完成情况即可。`archived` 表示已经回读确认，`generated` 表示本地出稿，`archive_failed` 表示报告可在本地交付。各项目的 `checked.json` 是成稿回执，`archive.json` 记录归档验证。参见 [批量处理](batch-processing.md) 和 [材料位置](material-handling.md)。
