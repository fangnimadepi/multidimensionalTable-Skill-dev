"""Receive complete SSE chat JSON without exposing partial or reasoning text."""
from __future__ import annotations

import json
import time


def read_chat_stream(response, timeout, heartbeat=None):
    parts = []; usage = {}; finish = None; done = False; chunks = 0; characters = 0
    started = last_notice = time.monotonic(); first_content = None
    for raw in response:
        now = time.monotonic()
        if now - started > timeout:
            raise TimeoutError('Model stream exceeded its overall deadline')
        line = raw.decode('utf-8').strip()
        if not line.startswith('data:'):
            continue
        value = line[5:].strip()
        if value == '[DONE]':
            done = True; break
        packet = json.loads(value)
        if packet.get('error'):
            raise RuntimeError('SiliconFlow stream returned an error; inspect the account privately')
        if packet.get('usage'):
            usage = packet['usage']
        for choice in packet.get('choices', []):
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta') or {}
            content = delta.get('content') or ''
            # reasoning_content is deliberately ignored and never displayed.
            if not isinstance(content, str):
                raise ValueError('Model stream content has an invalid shape')
            if content:
                if first_content is None: first_content = now-started
                parts.append(content); characters += len(content); chunks += 1
                if characters > 256000:
                    raise ValueError('Model stream content exceeds the output bound')
            if choice.get('finish_reason') is not None:
                finish = choice['finish_reason']
        if heartbeat and (chunks == 1 or now - last_notice >= 10):
            heartbeat(chunks, characters); last_notice = now
    if not done or finish is None:
        raise TimeoutError('Model stream ended before completion; partial text is not accepted')
    return {'choices': [{'message': {'content': ''.join(parts)}, 'finish_reason': finish}],
            'usage': {**usage, 'streamed': True, 'stream_chunks': chunks, 'first_content_seconds': first_content}}
