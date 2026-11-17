"""OpenAI-format JSON calls with bounded retries and timings."""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

from llm_runtime import emit, request_slot, check_cancelled
from siliconflow_stream import read_chat_stream


def request_payload(system, user, config, tokens):
    encoded_user = json.dumps(user, ensure_ascii=False)
    # DeepSeek enforces this JSON-mode precondition, including on audit prompts
    # written entirely in Chinese. It changes formatting, not review criteria.
    if 'json' not in (system + encoded_user).lower():
        system += '\n请仅输出一个有效 JSON 对象，不附加 Markdown。'
    value = {'model': config['text_model'], 'messages': [
        {'role':'system','content':system}, {'role':'user','content':encoded_user}],
        'max_tokens': tokens, 'temperature': 0,
        'stream': config.get('stream', os.environ.get('SILICONFLOW_STREAM_JSON') == '1'),
        'response_format': {'type':'json_object'}}
    if config.get('provider') == 'deepseek':
        value['thinking'] = {'type': 'disabled'}
    else:
        value['enable_thinking'] = False
    if config.get('provider') == 'dashscope' and value['stream']:
        value['stream_options'] = {'include_usage': True}
    return value


def complete_json(system, user, config, tokens, credential):
    if not credential: raise RuntimeError('Configured model API key is unavailable')
    payload = request_payload(system, user, config, tokens)
    provider = config.get('provider', 'siliconflow')
    timeout = float(config.get('request_timeout_seconds', 150))
    idle = float(config.get('request_idle_timeout_seconds', timeout))
    attempts = int(config.get('request_attempts', 2))
    json_repairs = int(config.get('request_json_repair_attempts', 1))
    budget = float(config.get('request_total_budget_seconds', min(timeout * attempts, 900)))
    if not (1 <= timeout <= 900 and 1 <= idle <= 900 and 1 <= budget <= 1800 and 1 <= attempts <= 4 and 0 <= json_repairs <= 1):
        raise ValueError('Invalid model request deadline or retry policy')
    with request_slot(config) as queue_seconds:
        started = time.monotonic(); deadline = started + budget
        emit('request_start', provider=provider, model=config['text_model'], limiterWaitSeconds=queue_seconds)
        network_failures = 0; repaired_json = 0; control_chars_normalized = False
        for attempt in range(attempts + json_repairs):
            check_cancelled()
            result = {}; remaining = deadline-time.monotonic()
            if remaining <= 0: raise TimeoutError('模型调用超过总等待时限，已停止重试。')
            attempt_started = time.monotonic()
            request = urllib.request.Request(config['api_base'].rstrip('/')+'/chat/completions',
                data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={'Authorization':'Bearer '+credential,'Content-Type':'application/json'},method='POST')
            try:
                with urllib.request.urlopen(request, timeout=min(idle, timeout, remaining)) as response:
                    header_seconds = time.monotonic()-attempt_started
                    # A request's deadline includes connection, wait, and output.
                    available = min(timeout, deadline-time.monotonic())
                    if available <= 0: raise TimeoutError('模型响应超过等待时限。')
                    def abort_response():
                        try:
                            sock = response.fp.raw._sock
                            sock.shutdown(socket.SHUT_RDWR)
                        except (AttributeError, OSError):
                            try: response.close()
                            except Exception: pass
                    timer = threading.Timer(available, abort_response); timer.daemon=True; timer.start()
                    try:
                        if payload['stream']:
                            result = read_chat_stream(response, available,
                                lambda chunks, chars: emit('request_progress', receivedCharacters=chars,
                                    elapsedSeconds=round(time.monotonic()-attempt_started, 2)))
                        else:
                            result = json.load(response)
                        if time.monotonic() > deadline: raise TimeoutError('模型响应超过总等待时限。')
                    finally: timer.cancel()
                raw = result['choices'][0]['message']['content']
                if result['choices'][0].get('finish_reason') == 'length':
                    raise json.JSONDecodeError('Model output was truncated', raw or '', len(raw or ''))
                if isinstance(raw, str) and raw.lstrip().startswith('```'):
                    raw = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', raw.strip())
                try:
                    answer = json.loads(raw)
                except json.JSONDecodeError as error:
                    # JSON-mode models sometimes emit a literal newline inside
                    # a string. Accept its exact string value, not guessed fields
                    # or reconstructed/truncated content. All source audits follow.
                    if error.msg.startswith('Invalid control character'):
                        answer = json.loads(raw, strict=False)
                        control_chars_normalized = True
                    else:
                        raise
                if not isinstance(answer, dict): raise json.JSONDecodeError('Expected JSON object', raw, 0)
                usage = dict(result.get('usage') or {})
                wall = time.monotonic()-started
                first_content = usage.get('first_content_seconds')
                if first_content is not None: usage['first_token_seconds'] = round(header_seconds+first_content, 3)
                usage.update(request_attempts=attempt+1,requested_max_tokens=tokens,
                    effective_max_tokens=payload['max_tokens'],json_protocol_retries=repaired_json,
                    json_control_chars_normalized=control_chars_normalized,
                    wall_seconds=round(wall, 3),header_seconds=round(header_seconds, 3),
                    limiter_wait_seconds=queue_seconds,provider=provider,model=config['text_model'])
                output_seconds = max(.001, time.monotonic()-attempt_started-(usage.get('first_token_seconds') or header_seconds))
                if usage.get('completion_tokens'): usage['output_tokens_per_second']=round(usage['completion_tokens']/output_seconds, 2)
                emit('request_end', provider=provider, model=config['text_model'], usage=usage)
                return answer, usage
            except urllib.error.HTTPError as error:
                if error.code not in {429,500,502,503,504}:
                    raise RuntimeError(f'{provider} HTTP {error.code}; inspect account/model privately') from None
                category = 'HTTP '+str(error.code)
                network_failures += 1
                can_retry = network_failures < attempts
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.RemoteDisconnected, ConnectionError) as error:
                category = type(error).__name__
                if isinstance(error, json.JSONDecodeError):
                    choice = result.get('choices', [{}])[0]
                    emit('response_format_error', reason=error.msg, position=error.pos,
                         receivedCharacters=len(choice.get('message', {}).get('content') or ''),
                         finishReason=choice.get('finish_reason'))
                    can_retry = repaired_json < json_repairs
                    if can_retry:
                        repaired_json += 1
                        # Formatting only: retain the same business input and all
                        # downstream gates. Never publish a partial JSON object.
                        payload['messages'][0]['content'] += '\n上次响应不是完整有效JSON。本次只输出完整JSON对象，正确转义字符串中的双引号、换行和反斜杠，不用代码围栏，不省略任何必需字段。'
                        if choice.get('finish_reason') == 'length':
                            payload['max_tokens'] = max(payload['max_tokens'], min(payload['max_tokens'] + 2000, 16000))
                else:
                    network_failures += 1
                    can_retry = network_failures < attempts
            will_retry = can_retry and attempt+1 < attempts+json_repairs and deadline-time.monotonic() > 1
            emit('request_retry' if will_retry else 'request_failed', provider=provider, attempt=attempt+1, reason=category)
            if not will_retry:
                raise RuntimeError(f'{provider} 模型调用未完成（{category}），已达到重试或等待上限。') from None
            time.sleep(min(2**attempt, max(0, deadline-time.monotonic())))
    raise AssertionError('Unreachable model call')
