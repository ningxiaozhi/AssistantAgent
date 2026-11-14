"""全链路 trace。

设计只有一条标准：出事时能不能回答这四个问题（架构文档 §6.4）
  1. 它当时看到了什么？   → payload 里的 context / retrieved
  2. 它为什么这么选？     → plan / reasoning 摘要
  3. 外部真实发生了什么？ → tool result / error
  4. 花了多少、慢在哪？   → tokens / cost / latency

这里只做「结构化事件 + 可落库」的最小实现；
接 LLM 时把 request/response 塞进 payload 即可，大对象走 blob 引用。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TraceEvent:
    kind: str  # task.start / model.call / tool.call / permission.ask / task.end ...
    task_id: str
    step_no: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "step_no": self.step_no,
            "payload": self.payload,
            "at_ms": self.at_ms,
        }


class TraceSink(Protocol):
    async def write(self, event: TraceEvent) -> None: ...


class NullSink:
    async def write(self, event: TraceEvent) -> None:  # noqa: D401
        return None


class Trace:
    """一次任务一条 trace，trace_id == task_id。"""

    def __init__(self, task_id: str | None = None, sink: TraceSink | None = None) -> None:
        self.trace_id = task_id or uuid.uuid4().hex
        self.sink = sink or NullSink()
        self.events: list[TraceEvent] = []
        self.step_no = 0

    def next_step(self) -> int:
        self.step_no += 1
        return self.step_no

    async def record(self, kind: str, **payload: Any) -> None:
        ev = TraceEvent(kind=kind, task_id=self.trace_id, step_no=self.step_no, payload=payload)
        self.events.append(ev)
        await self.sink.write(ev)

    def timeline(self) -> list[tuple[int, str, str]]:
        """给报告/调试用的极简时间线：(step, kind, 一句话摘要)。"""
        out: list[tuple[int, str, str]] = []
        for e in self.events:
            brief = ", ".join(f"{k}={v}" for k, v in list(e.payload.items())[:3])
            out.append((e.step_no, e.kind, brief))
        return out
