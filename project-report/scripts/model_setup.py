#!/usr/bin/env python3
"""Configure the report model without putting its key in command arguments."""

import argparse
import getpass
import json
import os
import re
import sys
import tempfile
import urllib.error
from urllib.parse import urlsplit
from pathlib import Path


LOCAL_MODEL_CONFIG = Path(__file__).resolve().parent.parent / "config/model.local.json"
MANUAL_KEY_ENV = "JINGYAN_REPORT_API_KEY"


class ModelSetupError(ValueError):
    def __init__(self, message, code, *, status=None, retryable=False):
        super().__init__(message)
        self.code, self.http_status, self.retryable = code, status, retryable

    def details(self):
        return {"error_code": self.code, "retryable": self.retryable,
                **({"http_status": self.http_status} if self.http_status else {})}


def manual_model_options(providers, use_credentials):
    path = LOCAL_MODEL_CONFIG
    if path.is_symlink():
        raise ValueError("手动模型配置需为技能目录内的普通文件。")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("config/model.local.json 应为 JSON 文件。")
    try:
        values = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        raise ValueError("config/model.local.json 格式错误，请检查 JSON。") from None
    if not isinstance(values, dict):
        raise ValueError("手动模型配置应包含 api_base、model 和 api_key。")
    base = values.get("api_base")
    if not isinstance(base, str) or not re.fullmatch(r"[!-~]{1,2048}", base) or "\\" in base:
        raise ValueError("api_base 应为有效的模型接口地址。")
    try:
        url = urlsplit(base)
        host = url.hostname
        port = url.port
    except ValueError:
        raise ValueError("api_base 的主机或端口格式错误。") from None
    if (not host or port == 0 or url.username is not None or url.password is not None or "?" in base or "#" in base
            or (url.scheme != "https" and (url.scheme != "http" or host not in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("api_base 应使用 HTTPS 或本机回环 HTTP，且不含账号、查询串或片段。")
    base = base.rstrip("/").removesuffix("/chat/completions").rstrip("/")
    model = values.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[!-~]{1,200}", model):
        raise ValueError("请在 config/model.local.json 填写模型接口所用的 model 名称。")
    if use_credentials:
        key = values.get("api_key")
        if not isinstance(key, str) or not re.fullmatch(r"[!-~]{1,4096}", key):
            raise ValueError("请在 config/model.local.json 填写原始 api_key，避免空格或 Bearer 前缀。")
        os.environ[MANUAL_KEY_ENV] = key
    provider = next((name for name, info in providers.items() if urlsplit(info[1]).hostname == host),
                    "openai_compatible")
    options = {"provider": provider, "api_base": base, "text_model": model, "api_key_env": MANUAL_KEY_ENV,
               "manual_endpoint": True}
    if provider == "openai_compatible":
        options["stream"] = False
    return options


def credential_path(config_file, runtime):
    return (config_file.parent / runtime.get("credentials_file", "local/jingyan-model.env")).absolute()


def read_credentials(path):
    allowed = {"DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "SILICONFLOW_API_KEY"}
    if path.is_symlink():
        raise ValueError("密钥文件不能使用符号链接。")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("本机密钥文件权限应为仅当前用户可读写。")
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in allowed or name in values:
            raise ValueError("本机模型密钥文件格式不正确。")
        values[name] = value.strip()
    return values


def load_credentials(config_file, runtime, key_env):
    """An explicit user credential file takes precedence over the old shared .env."""
    if not runtime.get("credentials_file"):
        return
    path = credential_path(config_file, runtime)
    if not path.is_file():
        raise ValueError("鲸研的个人模型密钥文件不存在，请先配置报告模型。")
    key = read_credentials(path).get(key_env)
    if not key:
        raise ValueError("鲸研的个人模型密钥文件缺少当前平台的密钥，请先配置。")
    from private_model_settings import _credentials

    _credentials({"provider": runtime["provider"], "apiKey": key})
    os.environ[key_env] = key


def verify_connection(options, key):
    from llm_client import ModelRequestError, complete_json

    try:
        response, _ = complete_json('Return JSON {"ok":true}.', {},
                                   {**options, "stream": False, "request_attempts": 1,
                                    "request_json_repair_attempts": 1, "request_timeout_seconds": 20,
                                    "request_idle_timeout_seconds": 20, "request_total_budget_seconds": 20},
                                   128, key)
    except ModelRequestError as error:
        messages = {
            "authentication": "模型密钥验证未通过（HTTP 401），请在模型设置中核对密钥。",
            "balance": "模型账号余额不足（HTTP 402），请充值或选择其他已配置的模型。",
            "forbidden": "当前账号没有该模型的访问权限（HTTP 403），请核对平台及模型权限。",
            "rate_limit": "模型请求达到限额（HTTP 429），请稍后重试。",
            "server": "模型平台暂时不可用，请稍后重试。",
            "tls": "模型接口的安全连接验证失败，请检查网络或证书配置。",
            "timeout": "模型验证请求超时，请稍后重试。",
            "connection": "未能连接模型接口，请检查网络和接口地址。",
            "response_format": "模型响应格式不完整或不支持 JSON 输出，请核对模型及接口协议。",
        }
        raise ModelSetupError(messages.get(error.category, "模型接口验证失败，请核对接口及模型权限。"),
                              "model_" + error.category, status=error.http_status,
                              retryable=error.retryable) from None
    except urllib.error.HTTPError as error:
        raise ModelSetupError(f"模型验证未通过，HTTP {error.code}，请检查模型账号。",
                              "model_http", status=error.code) from None
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
        raise ValueError("模型连接或响应验证未完成，请检查接口地址、模型权限和密钥后重试。") from None
    if response.get("ok") is not True:
        raise ModelSetupError("模型未返回预期的验证结果，请确认模型支持 JSON 响应。", "model_response_format")


def save_model(config_file, config, provider, model, key):
    from local_reports import setup_runtime

    if LOCAL_MODEL_CONFIG.exists() or LOCAL_MODEL_CONFIG.is_symlink():
        raise ValueError("当前技能使用 config/model.local.json。请编辑该文件，保存后用 check-model 检查连接。")
    candidate = json.loads(json.dumps(config))
    runtime = candidate.setdefault("report_runtime", {})
    runtime.update(provider=provider, model=model, model_source="configured")
    _, options = setup_runtime(config_file, candidate, use_credentials=False)
    from private_model_settings import _credentials
    from private_files import protect_private_file
    from review_common import atomic_write_json

    _credentials({"provider": provider, "apiKey": key})
    verify_connection(options, key)
    path = credential_path(config_file, runtime)
    previous = read_credentials(path) if path.exists() else {}
    previous[options["api_key_env"]] = key
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write("# 鲸研个人报告模型配置。仅保存在本机。\n")
            stream.write("".join(f"{name}={value}\n" for name, value in sorted(previous.items())))
        protect_private_file(temporary)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    runtime["credentials_file"] = str(path.relative_to(config_file.parent)) if path.is_relative_to(config_file.parent) else str(path)
    atomic_write_json(config_file, candidate)
    return {"status": "configured", "provider": provider, "model": model,
            "connection_verified": True, "config_file": str(config_file), "credentials_file": str(path)}


def bind_workbuddy(config_file, config, selected=None, explicit=None):
    from local_reports import setup_runtime
    from workbuddy_models import choose

    path, model_id, model, _, _ = choose(explicit, selected)
    # Only the reference is persisted; no copy of WorkBuddy's API Key is made.
    candidate = json.loads(json.dumps(config))
    target = candidate.setdefault("report_runtime", {})
    target.update(model_source="workbuddy", workbuddy_config=str(path), workbuddy_model_id=model_id)
    _, checked_options = setup_runtime(config_file, candidate)
    from review_common import atomic_write_json

    verify_connection(checked_options, os.environ[checked_options["api_key_env"]])
    atomic_write_json(config_file, candidate)
    return {"status": "configured", "model_source": "workbuddy", "model": model,
            "connection_verified": True, "config_file": str(config_file)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--provider", choices=("deepseek", "dashscope", "siliconflow"))
    mode.add_argument("--from-workbuddy", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--workbuddy-config", type=Path)
    parser.add_argument("--list", action="store_true", help="仅列出可复用的 WorkBuddy 模型名称")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--key-file", type=Path)
    source.add_argument("--key-stdin", action="store_true")
    source.add_argument("--key-prompt", action="store_true")
    args = parser.parse_args()
    try:
        config_file = args.config.resolve(strict=True)
        config = json.loads(config_file.read_text(encoding="utf-8"))
        if args.from_workbuddy:
            if args.key_file or args.key_stdin or args.key_prompt:
                parser.error("复用 WorkBuddy 时无需再次提供密钥。")
            if args.list:
                from workbuddy_models import available_models
                _, choices = available_models(args.workbuddy_config)
                result = {"status": "models", "candidates": choices}
            else:
                result = bind_workbuddy(config_file, config, args.model, args.workbuddy_config)
        else:
            if not args.model or args.list or args.workbuddy_config or not (args.key_file or args.key_stdin or args.key_prompt):
                parser.error("独立配置需指定 --model 及一种密钥输入方式。")
            if args.key_file:
                key = args.key_file.read_text(encoding="utf-8-sig").strip()
            elif args.key_stdin:
                key = sys.stdin.read(1024).strip()
            else:
                key = getpass.getpass("请粘贴报告模型 API Key（输入隐藏）：").strip()
            result = save_model(config_file, config, args.provider, args.model, key)
    except (OSError, ValueError, KeyError, ImportError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error),
                          **(error.details() if callable(getattr(error, "details", None)) else {})}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
