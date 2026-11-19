# 默认模型与可选 API 配置

## 默认：WorkBuddy 当前会话模型

鲸研默认不需要配置报告 API。用户在 WorkBuddy 选择内置积分模型或自定义模型，选中「鲸研项目报告」并说明任务即可。两者都使用相同的宿主执行流程，计费由 WorkBuddy 和所选模型服务方按正常规则处理。

默认流程不读取 Skill 的 model.local.json、密钥文件或 WorkBuddy 的 models.json。即使旧 API 配置存在或格式有误，也不会自动启用。模型生成内容由当前会话完成，脚本负责材料解析、原文/数字/结构检查、Word 导出及飞书归档。具体协议见 [宿主执行](host-workflow.md)。

## 可选：独立 API 模式

用户明确说「用 Skill 中单独配置的 API 生成报告」时，才在初次命令加 --mode api。宿主仍负责执行工具，报告正文由独立 API 链路生成；该 API 按对应平台规则另行计费。脚本不能提取 WorkBuddy 内置模型的订阅凭据来调用 API。宿主模式失败时不得自动切换本模式。

在实际导入的 Skill 目录，将 config/model.example.json 复制为 config/model.local.json，用户在本机填写：

    {
      "api_base": "https://api.deepseek.com",
      "model": "deepseek-flash",
      "api_key": "在本机填写实际密钥"
    }

api_base 使用供应商的 OpenAI Chat Completions 基础地址或以 /chat/completions 结尾的完整地址；远端使用 HTTPS。模型名填 API 所需 ID。个人文件和密钥不会进入发布包。

只在配置独立 API 后，使用 scripts/run.sh check-model（Windows 对应 scripts/run.ps1）验证它。验证区分余额、密钥、权限、限流、TLS、格式及连接错误，不打印平台原始响应；仅格式错误最多补试一次，等待有上限。check-model 不是检查 WorkBuddy 内置模型的方法，默认模式不执行它。

本地材料明确使用独立 API 的命令为 generate --mode api；飞书任务为 feishu deliver --mode api，或 inspect --mode api --archive。模式会记入批次，续跑不自行改换模式。

## 高级配置与旧配置兼容

原有通过 configure --provider ... --model ... --key-file ... 保存的个人配置仍可在 API 模式使用，密钥文件在个人配置旁的 local/jingyan-model.env；也支持用户终端中的 --key-stdin 或 --key-prompt。只需手动文件时，model.local.json 支持自定义接口及模型 ID。已明确绑定 WorkBuddy 的 API 配置会保持原选择，切换独立来源时应明确配置对应平台。

如果用户明确选择 API 模式、并希望复用 WorkBuddy 的自定义 API 配置，可用 configure --from-workbuddy --list 列候选，再用 configure --from-workbuddy --model '选中的模型ID或显示名称' 保存引用。脚本只保存配置路径及模型 ID，不复制密钥，每次 API 运行读取最新值。这仅适用于兼容的自定义 Chat Completions 模型，不包括内置积分模型；不会跟随会话临时切换模型。

配置、.env 和 WorkBuddy models.json 都只能由专门脚本读取。宿主不得打印、复制到会话上下文或自行写诊断请求。权限不足时请用户确认对应的读取或联网权限；原配置保留，不以放宽安全策略或不断更换工具绕过。
