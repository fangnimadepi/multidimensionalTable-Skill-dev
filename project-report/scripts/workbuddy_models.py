"""Read one explicitly selected WorkBuddy custom model without exporting its key."""

import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

KEY_ENV = "JINGYAN_WORKBUDDY_API_KEY"


def setting(value):
    if not isinstance(value, str):
        return value
    def replace(match):
        result = os.environ.get(match[1])
        if result is None:
            raise ValueError("模型配置引用的环境变量尚未设置")
        return result
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


class WorkBuddyModelError(ValueError):
    def __init__(self, message, code, candidates=None):
        super().__init__(message)
        self.code, self.candidates = code, candidates or []

    def details(self):
        return {"error_code": self.code, "retryable": False,
                **({"candidates": self.candidates} if self.candidates else {})}


def config_paths():
    return [Path.home() / ".workbuddy/models.json", Path.home() / ".codebuddy/models.json"]


def model_file(explicit=None):
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise WorkBuddyModelError("WorkBuddy 配置路径须为绝对路径。", "workbuddy_config_invalid")
        return path
    return next((path for path in config_paths() if path.is_file()), None)


def read_models(explicit=None):
    path = model_file(explicit)
    if path is None or not path.is_file():
        raise WorkBuddyModelError("请先在 WorkBuddy 的模型设置中配置自定义模型，或使用鲸研手动模型配置。",
                                 "workbuddy_config_missing")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except PermissionError:
        raise WorkBuddyModelError("需要允许鲸研读取 WorkBuddy 的本机模型配置；确认权限后再继续。",
                                 "workbuddy_config_permission_required") from None
    except (OSError, ValueError):
        raise WorkBuddyModelError("WorkBuddy 模型配置无法读取，请在 WorkBuddy 设置中检查后重试。",
                                 "workbuddy_config_invalid") from None
    rows = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise WorkBuddyModelError("当前 WorkBuddy 模型配置格式暂不支持；可使用鲸研手动配置。",
                                 "workbuddy_config_unsupported")
    return path, rows


def endpoint(row):
    base = setting(row.get("url"))
    if not isinstance(base, str) or not re.fullmatch(r"[!-~]{1,2048}", base) or "\\" in base:
        raise ValueError("接口地址不完整")
    try:
        parsed = urlsplit(base)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("接口地址不正确") from None
    if (not host or port == 0 or parsed.username is not None or parsed.password is not None
            or "?" in base or "#" in base
            or parsed.scheme != "https" and (parsed.scheme != "http" or host not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("接口须为 HTTPS 或本机回环 HTTP，且不含查询参数或账号")
    base = base.rstrip("/")
    if row.get("useCustomProtocol") and not base.endswith("/chat/completions"):
        raise ValueError("当前自定义协议不是可复用的 Chat Completions 接口")
    return base.removesuffix("/chat/completions").rstrip("/")


def identity(row):
    # In the documented models.json schema, id is the API model identifier;
    # name is an optional display label and can contain spaces or Chinese.
    key = row.get("id")
    if not isinstance(key, str) or not re.fullmatch(r"[!-~]{1,200}", key):
        raise ValueError("缺少接口使用的模型 ID")
    return key, key


def available_models(explicit=None):
    """Return only allowlisted display fields; never URLs or raw config data."""
    path, rows = read_models(explicit)
    choices = []
    for row in rows:
        try:
            key, model = identity(row)
            endpoint(row)
            secret = setting(row.get("apiKey"))
            if not isinstance(secret, str) or not re.fullmatch(r"[!-~]{1,4096}", secret):
                continue
        except ValueError:
            continue
        display = row.get("name") if isinstance(row.get("name"), str) else model
        choices.append({"id": key, "model": model, "name": " ".join(display.split())[:120]})
    return path, choices


def choose(explicit=None, selected=None):
    path, rows = read_models(explicit)
    if selected is not None:
        matches = [row for row in rows if selected in (row.get("id"), row.get("name"))]
    else:
        _, options = available_models(path)
        usable = {item["id"] for item in options}
        matches = [row for row in rows if row.get("id") in usable]
    if len(matches) != 1:
        _, options = available_models(path)
        code = "workbuddy_model_selection_required" if options else "workbuddy_model_unavailable"
        raise WorkBuddyModelError("请选择用于鲸研报告的 WorkBuddy 自定义模型。" if options else
                                 "未找到可复用的自定义模型；内置模型凭据不由本适配器提供，请在 WorkBuddy 设置中添加自定义模型。",
                                 code, options)
    row = matches[0]
    try:
        key, model = identity(row)
        base = endpoint(row)
        secret = setting(row.get("apiKey"))
        if not isinstance(secret, str) or not re.fullmatch(r"[!-~]{1,4096}", secret):
            raise ValueError("当前模型缺少可读取的原始 API Key")
    except ValueError as error:
        raise WorkBuddyModelError("当前模型暂不能供鲸研脚本复用：" + str(error) + "。",
                                 "workbuddy_model_unsupported") from None
    return path, key, model, base, secret


def model_options(runtime, providers, use_credentials=True):
    path, key, model, base, secret = choose(runtime.get("workbuddy_config"), runtime.get("workbuddy_model_id"))
    provider = next((name for name, info in providers.items() if urlsplit(info[1]).hostname == urlsplit(base).hostname),
                    "openai_compatible")
    if use_credentials:
        os.environ[KEY_ENV] = secret
    options = {"provider": provider, "api_base": base, "text_model": model, "api_key_env": KEY_ENV,
               "manual_endpoint": True, "stream": False, "model_source": "workbuddy",
               "model_config_file": str(path), "workbuddy_model_id": key}
    if model.lower().startswith("qwen"):
        options["enable_thinking"] = False
    return options
