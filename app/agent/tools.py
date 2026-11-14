"""工具层：最小可用的两个工具 + 统一的副作用契约。

每个工具必须声明三件事，否则不允许注册：
  side_effect    none / reversible / external  —— 决定取消时要不要补偿
  idempotency   幂等键模板 —— 重试、重复取消、进程恢复不会执行两次
  permission    L0..L3  —— 决定要不要经过确认闸门
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

NONE = "none"
REVERSIBLE = "reversible"
EXTERNAL = "external"


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    side_effect: str = NONE


@dataclass
class Tool:
    name: str
    description: str
    side_effect: str
    permission: str  # L0 | L1 | L2 | L3
    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    idempotency: str = "{task_id}:{tool}:{hash}"  # 幂等键模板

    def key(self, task_id: str, args: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(args, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:12]
        return self.idempotency.format(task_id=task_id, tool=self.name, hash=digest)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.side_effect not in (NONE, REVERSIBLE, EXTERNAL):
            raise ValueError(f"{tool.name}: 非法 side_effect {tool.side_effect}")
        if tool.permission not in ("L0", "L1", "L2", "L3"):
            raise ValueError(f"{tool.name}: 非法权限等级 {tool.permission}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, Any]]:
        """给模型看的工具清单（不含实现）。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "side_effect": t.side_effect,
                "permission": t.permission,
            }
            for t in self._tools.values()
        ]


# ---------------------------------------------------------------- 具体工具
async def _notes_create(args: dict[str, Any]) -> ToolResult:
    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, error="空笔记", error_code="bad_args")
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    record = {
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "tag": args.get("tag", "inbox"),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with (data_dir / "notes.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return ToolResult(ok=True, data=record, side_effect=REVERSIBLE)


async def _time_now(args: dict[str, Any]) -> ToolResult:
    return ToolResult(
        ok=True,
        data={"now": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        side_effect=NONE,
    )


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="notes.create",
            description="把一句话记进收件箱，不需要确认",
            side_effect=REVERSIBLE,
            permission="L1",
            handler=_notes_create,
        )
    )
    reg.register(
        Tool(
            name="time.now",
            description="取当前时间。注意：评测时必须用冻结时间替代",
            side_effect=NONE,
            permission="L0",
            handler=_time_now,
        )
    )
    return reg
