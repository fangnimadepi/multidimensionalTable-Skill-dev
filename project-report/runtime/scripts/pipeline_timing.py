"""Thread-safe live progress and timing log, excluding prompts, outputs and secrets."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from review_common import atomic_write_json


def stage_label(report, stage):
    if stage.startswith("grouped-"):
        if stage.startswith("grouped-reader-write"):
            return "履约审查：整理项目概况与实际进展"
        if stage.startswith("grouped-basis-write"):
            return "履约审查：核对成果与计划口径"
        if stage.startswith("grouped-initial-audit"):
            return "履约审查：核对概况、进展及成果口径"
        if stage.startswith("grouped-final-write"):
            return "履约审查：整理综合意见与PI确认事项"
        if stage.startswith("grouped-final-audit"):
            return "履约审查：核对正文与原文支持关系"
    if report == "compact":
        return {
            "extract": "解析两份材料，建立共用原文索引",
            "review-draft": "对照计划并撰写履约审查",
            "achievement-draft": "梳理成果并撰写价值分析",
            "independent-audit": "独立复核原文、阶段口径及两份报告",
            "audit-adjudication": "核实复核意见，区分实质错误与措辞建议",
            "targeted-repair": "根据复核意见修订相关段落",
        }.get(stage, "整理项目报告")
    if report == "achievement":
        label = (
            "提炼并核验成果事实" if stage.startswith("facts") else "撰写并复核成果分析"
        )
        if stage.startswith("paired"):
            label = "交叉核对两类报告"
        if stage == "extract":
            label = "解析成果与背景材料"
        return "成果分析：" + label
    if stage == "inventory":
        label = "拆解任务书计划与考核条件"
    elif stage == "extract":
        label = "解析任务书计划与总结报告"
    elif stage.startswith(("alignment", "numeric")):
        label = "对照计划与实际进展"
    elif stage.startswith("assurance"):
        label = "核对成果状态与认定口径"
    elif stage.startswith("evidence"):
        label = "核验依据原文及上下文"
    elif stage.startswith("confirmation"):
        label = "整理与PI的确认事项"
    else:
        label = "撰写并复核履约审查报告"
    return "履约审查：" + label


class PipelineTrace:
    def __init__(self, folder, attempt_id=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.active = {}
        self.completed = []
        self.calls = []
        self.cache_hits = 0
        self.message = "读取项目材料，检查文件版本"
        self.io_failures = 0
        self.ocr_events = []
        self.attempt_id = attempt_id or uuid.uuid4().hex

    def write(self, name, value):
        try:
            atomic_write_json(self.folder / name, value)
        except OSError:
            self.io_failures += 1  # Progress is advisory, never a report gate.

    def summary(self):
        with self.lock:
            return {
                "attemptId": self.attempt_id,
                "elapsedSeconds": round(time.monotonic() - self.started, 2),
                "completedCalls": len(self.calls),
                "progressIoFailures": self.io_failures,
                "cachedSteps": self.cache_hits,
                "activeSteps": list(self.active.values()),
                "apiWallSumSeconds": round(
                    sum(x.get("usage", {}).get("wall_seconds", 0) for x in self.calls),
                    2,
                ),
                "steps": list(self.completed),
                "ocr": list(self.ocr_events),
            }

    def progress(self):
        labels = list(dict.fromkeys(value["label"] for value in self.active.values()))
        self.write(
            "progress.json",
            {
                "message": "；".join(labels) or self.message,
                "stage": next(iter(self.active), "waiting"),
                "updatedAt": time.time(),
                "timing": self.summary(),
            },
        )

    def phase(self, message, stage):
        with self.lock:
            self.message = message
            self.progress()

    def event(self, event):
        # Explicit allowlist: trace files cannot accidentally capture model text.
        safe = {
            key: event[key]
            for key in (
                "event",
                "time",
                "report",
                "stage",
                "provider",
                "model",
                "durationSeconds",
                "errorType",
                "limiterWaitSeconds",
                "receivedCharacters",
                "elapsedSeconds",
                "attempt",
                "reason",
                "usage",
                "position",
                "finishReason",
                "page",
                "pages",
                "backend",
                "cache_hit",
                "characters",
            )
            if key in event
        }
        safe["attemptId"] = self.attempt_id
        key = event.get("report", "") + "/" + event.get("stage", "")
        kind = event["event"]
        with self.lock:
            try:
                with (self.folder / "timing-events.jsonl").open(
                    "a", encoding="utf-8"
                ) as stream:
                    stream.write(json.dumps(safe, ensure_ascii=False) + "\n")
            except OSError:
                self.io_failures += 1
            if kind == "stage_start":
                self.active[key] = {
                    "stage": key,
                    "label": stage_label(
                        event.get("report", ""), event.get("stage", "")
                    ),
                    "startedAt": event["time"],
                }
            elif kind in {"stage_end", "stage_failed"}:
                self.active.pop(key, None)
                self.completed.append(safe)
            elif kind == "request_end":
                self.calls.append(safe)
            elif kind == "cache_reused":
                self.cache_hits += 1
            elif kind == "request_progress" and key in self.active:
                self.active[key].update(
                    receivedCharacters=event.get("receivedCharacters"),
                    requestElapsedSeconds=event.get("elapsedSeconds"),
                )
            elif kind.startswith("ocr_"):
                if kind in {"ocr_start", "ocr_end", "ocr_cache_hit"}:
                    self.ocr_events.append(safe)
                if key in self.active:
                    if kind == "ocr_progress":
                        self.active[key][
                            "label"
                        ] = f"识别PDF文字：第{event['page']}/{event['pages']}页"
                    elif kind == "ocr_wait":
                        self.active[key]["label"] = "检查或等待PDF文字识别"
                    elif kind in {"ocr_end", "ocr_cache_hit"}:
                        self.active[key]["label"] = stage_label(
                            event.get("report", ""), event.get("stage", "")
                        )
            self.progress()
