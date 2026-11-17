"""Session-only BYOK. Credentials never enter manifests, caches or user records."""
import json
import re
import urllib.error
import urllib.request

from llm_client import request_payload

PROVIDERS = {
    'deepseek': ('DeepSeek 官网', 'https://api.deepseek.com', 'DEEPSEEK_API_KEY', ()),
    'dashscope': ('阿里云百炼', 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                  'DASHSCOPE_API_KEY', ('qwen-flash', 'qwen-plus')),
    'siliconflow': ('硅基流动', 'https://api.siliconflow.cn/v1', 'SILICONFLOW_API_KEY',
                    ('deepseek-ai/DeepSeek-V4.1-Flash',)),
}


def _credentials(value):
    if not isinstance(value, dict):
        raise ValueError('请先在个人设置中填写模型平台的 API Key 并选择模型。')
    provider = value.get('provider', 'deepseek')
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError('请选择支持的模型平台。')
    label, _, _, _ = PROVIDERS[provider]
    key = value.get('apiKey')
    pattern = r'sk-[A-Za-z0-9_.-]{16,512}' if provider == 'dashscope' else r'sk-[A-Za-z0-9_-]{16,240}'
    if not isinstance(key, str) or not re.fullmatch(pattern, key):
        raise ValueError(f'{label}密钥格式不正确，请检查是否多填了 Bearer 或空格。')
    return provider, key


def _supported(provider, model):
    if not isinstance(model, str):
        return False
    if provider == 'deepseek':
        return bool(re.fullmatch(r'deepseek-[A-Za-z0-9.-]{1,64}', model))
    return model in PROVIDERS[provider][3]


def settings(value, defaults):
    provider, key = _credentials(value)
    label, base, key_env, _ = PROVIDERS[provider]
    model = value.get('model')
    if not _supported(provider, model):
        raise ValueError(f'请选择{label}可用的模型。')
    return key, {**defaults, 'provider': provider, 'api_base': base,
                 'api_key_env': key_env, 'text_model': model,
                 'generation_mode': 'legacy', 'achievement_report_mode': 'expression'}


def _request(url, key, payload=None):
    headers = {'Authorization': 'Bearer ' + key}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, headers=headers,
                                     data=json.dumps(payload).encode() if payload is not None else None)
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('Model response exceeds validation bound')
    return json.loads(raw)


def _dashscope_models(key, base, candidates):
    # The official model-list endpoint requires a workspace ID. Verify inference
    # permission on the regional endpoint with tiny, synthetic JSON requests.
    models = []
    for model in candidates:
        payload = request_payload('Return the JSON object {"ok":true}.', {},
                                  {'provider': 'dashscope', 'text_model': model, 'stream': False}, 32)
        try:
            response = _request(base + '/chat/completions', key, payload)
            if json.loads(response['choices'][0]['message']['content']) == {'ok': True}:
                models.append(model)
        except urllib.error.HTTPError as error:
            if error.code not in {400, 403, 404}:
                raise
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            continue  # A failed probe must not hide the other verified model.
    return models


def available_models(value):
    provider, key = _credentials(value)
    label, base, _, candidates = PROVIDERS[provider]
    try:
        if provider == 'dashscope':
            models = _dashscope_models(key, base, candidates)
        else:
            result = _request(base + '/models', key)
            matched = {item['id'] for item in result['data'] if isinstance(item, dict)
                       and _supported(provider, item.get('id'))}
            models = [model for model in candidates if model in matched] if candidates else sorted(matched)
    except urllib.error.HTTPError as error:
        if error.code == 402:
            raise ValueError(f'{label}拒绝了验证请求，请检查账户余额或可用额度（HTTP 402）。') from None
        raise ValueError(f'{label}密钥验证未通过（HTTP {error.code}），请检查密钥和账号权限。') from None
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        raise ValueError(f'暂时无法验证{label}账号，请检查网络后重试。') from None
    if not models:
        raise ValueError(f'当前账号未通过{label}模型验证，请检查模型权限和可用额度。')
    return {'models': models}
