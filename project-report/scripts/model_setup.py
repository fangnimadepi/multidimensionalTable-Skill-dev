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
    from llm_client import complete_json

    try:
        response, _ = complete_json('Return JSON {"ok":true}.', {},
                                   {**options, "stream": False, "request_attempts": 1,
                                    "request_json_repair_attempts": 0, "request_timeout_seconds": 20,
                                    "request_idle_timeout_seconds": 20, "request_total_budget_seconds": 20},
                                   32, key)
    except urllib.error.HTTPError as error:
        raise ValueError(f"模型验证未通过，HTTP {error.code}，请检查平台、密钥、余额及模型权限。") from None
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError):
        raise ValueError("模型连接或响应验证未完成，请检查接口地址、模型权限和密钥后重试。") from None
    if response.get("ok") is not True:
        raise ValueError("模型未返回预期的验证结果，请确认模型支持 JSON 响应。")


def save_model(config_file, config, provider, model, key):
    from local_reports import setup_runtime

    if LOCAL_MODEL_CONFIG.exists() or LOCAL_MODEL_CONFIG.is_symlink():
        raise ValueError("当前技能使用 config/model.local.json。请编辑该文件，保存后用 check-model 检查连接。")
    candidate = json.loads(json.dumps(config))
    runtime = candidate.setdefault("report_runtime", {})
    runtime.update(provider=provider, model=model)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--provider", required=True, choices=("deepseek", "dashscope", "siliconflow"))
    parser.add_argument("--model", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-file", type=Path)
    source.add_argument("--key-stdin", action="store_true")
    source.add_argument("--key-prompt", action="store_true")
    args = parser.parse_args()
    try:
        config_file = args.config.resolve(strict=True)
        config = json.loads(config_file.read_text(encoding="utf-8"))
        if args.key_file:
            key = args.key_file.read_text(encoding="utf-8-sig").strip()
        elif args.key_stdin:
            key = sys.stdin.read(1024).strip()
        else:
            key = getpass.getpass("请粘贴报告模型 API Key（输入隐藏）：").strip()
        result = save_model(config_file, config, args.provider, args.model, key)
    except (OSError, ValueError, KeyError, ImportError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
