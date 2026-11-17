# 个人报告模型配置

先按 [独立运行与初始化](report-runtime.md) 完成首次环境准备。手动配置和对话配置都作用于鲸研的报告模型。

## 在技能目录手动配置

在实际导入的 `project-report` 目录中，将 `config/model.example.json` 复制为 `config/model.local.json`。如果 WorkBuddy 把 Skill 安装到另一份目录，以 `init` 回执里的 `manual_model_config` 路径为准。用记事本或 VS Code 打开个人配置，填写这三项。

```json
{
  "api_base": "https://api.deepseek.com",
  "model": "deepseek-flash",
  "api_key": "在本机粘贴实际密钥"
}
```

`api_base` 可填 API 基础地址，也可填以 `/chat/completions` 结尾的完整接口地址。按供应商的文档填写接口使用的模型 ID。自定义网关需要支持 OpenAI Chat Completions 接口及 JSON 输出模式。远端地址使用 HTTPS。

保存后下一次执行即会使用新值。可对 WorkBuddy 说「使用鲸研，检查我的报告模型连接」，或在技能目录中直接执行以下命令。

```powershell
& .\scripts\run.ps1 check-model
```

```bash
sh scripts/run.sh check-model
```

`check-model` 发送一条验证请求，输出接口是否可用、模型和当前配置文件路径。`doctor` 仅检查本机配置。让用户自己编辑 `model.local.json`，由脚本读取密钥。保留发布模板中的空密钥。Git 和发布脚本会排除个人配置，分享 Skill 时使用生成的发布包。

## 通过 WorkBuddy 对话配置

技能目录没有 `model.local.json` 时，可以说「帮我配置鲸研报告模型，用 DeepSeek Flash」。使用用户主目录中的个人配置及密钥文件。可以按用户要求创建并打开本机的私有文本文件，例如用户工作目录下 `local/待导入密钥.txt`。文件只含密钥本身，由配置脚本读取。

通过运行入口调用配置工具。`$ENTRY` 为 `scripts/run.sh` 的实际路径，`$KEY_FILE` 为用户刚保存的纯密钥文件路径。Windows 对应使用 `scripts/run.ps1`。平台与模型名按用户选择填写。

```bash
sh "$ENTRY" configure --provider deepseek --model deepseek-flash --key-file "$KEY_FILE"
```

也支持 `--key-stdin` 或用户可操作的终端中的 `--key-prompt`。密钥须通过上述输入方式提供。脚本执行一次最小模型请求，验证成功后保存平台、模型与本地密钥文件路径，密钥保存在个人配置旁的 `local/jingyan-model.env`。失败保留原配置，反馈平台、模型及可操作的错误原因，输出不含密钥。

对话配置的平台和模型选择由包内 `runtime/scripts/private_model_settings.py` 限定。模型名不受支持时说明限制。需要自选接口地址或模型 ID 时使用 `config/model.local.json`。配置文件存在但格式有误时，请修正提示的字段后重试。

每位用户使用自己的配置和密钥文件。配置存在而凭据缺失时先处理个人配置。个人凭据只供本机模型请求使用，工作底稿、报告与发布包采用各自独立目录。
