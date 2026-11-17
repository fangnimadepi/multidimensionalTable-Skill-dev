"""Bounded concurrency and content-free timing events shared by report generators."""
from __future__ import annotations

import contextlib
import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config/llm_runtime.json'
EXECUTION_KEYS = {'request_timeout_seconds', 'request_idle_timeout_seconds', 'request_total_budget_seconds',
    'request_attempts', 'request_json_repair_attempts', 'max_llm_concurrency', 'alignment_workers', 'report_workers', 'evidence_workers', 'audit_workers', 'stream',
    'generation_mode','review_workflow','achievement_report_mode','compact_writer_model','compact_audit_model','compact_adjudicator_model','compact_repair_model','generation_budget_seconds'}
_context = contextvars.ContextVar('model_call_context', default={})
_observer = None
_limits = {}; _lock = threading.Lock()
_cancelled = threading.Event()


def reset_cancellation(): _cancelled.clear()
def cancel_requests(): _cancelled.set()
def check_cancelled():
    if _cancelled.is_set(): raise RuntimeError('另一类报告处理未完成，本次已停止后续调用，已完成步骤保留。')


def model_config():
    path = CONFIG if CONFIG.exists() else ROOT / 'config/siliconflow_pilot.json'
    return json.loads(path.read_text())


def semantic_config(config):
    return {key: value for key, value in config.items() if key not in EXECUTION_KEYS}


def set_observer(observer):
    global _observer
    _observer = observer


def emit(kind, **values):
    if _observer:
        _observer({'event': kind, 'time': time.time(), **_context.get(), **values})


@contextlib.contextmanager
def call_scope(**values):
    # Instrumentation wrappers may nest the same report/stage. Count its
    # lifetime once, without losing genuinely different child stages.
    current = _context.get()
    if current.get('_scope_active') and all(current.get(key) == value for key, value in values.items()):
        yield
        return
    token = _context.set({**current, **values, '_scope_active': True})
    started = time.monotonic()
    emit('stage_start')
    try:
        yield
    except Exception as error:
        emit('stage_failed', durationSeconds=round(time.monotonic()-started, 3), errorType=type(error).__name__)
        raise
    else:
        emit('stage_end', durationSeconds=round(time.monotonic()-started, 3))
    finally:
        _context.reset(token)


@contextlib.contextmanager
def request_slot(config):
    check_cancelled()
    count = max(1, min(4, int(config.get('max_llm_concurrency', 3))))
    key = (config['api_base'], count)
    with _lock:
        semaphore = _limits.setdefault(key, threading.BoundedSemaphore(count))
    started = time.monotonic()
    with semaphore:
        check_cancelled()
        yield round(time.monotonic()-started, 3)


def parallel_map(function, items, workers=1):
    items = list(items)
    if not items: return []
    count = max(1, min(4, int(workers), len(items)))
    if count == 1: return [function(item) for item in items]
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix='report-step') as pool:
        futures = {pool.submit(function, item): index for index,item in enumerate(items)}
        results = [None] * len(items)
        try:
            for future in as_completed(futures): results[futures[future]] = future.result()
        except Exception:
            for future in futures: future.cancel()
            raise
        return results
