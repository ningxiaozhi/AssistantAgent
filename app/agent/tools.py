"""工具层：最小可用的两个工具 + 统一的副作用契约。

每个工具必须声明三件事，否则不允许注册：
  side_effect    none / reversible / external  —— 决定取消时要不要补偿
  idempotency   幂等键模板 —— 重试、重复取消、进程恢复不会执行两次
  permission    L0..L3  —— 决定要不要经过确认闸门
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

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


# handler 名 → 实现。config/tools.yaml 里的 handler 字段指向这里。
HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[ToolResult]]] = {
    "notes_create": _notes_create,
    "time_now": _time_now,
}

DEFAULT_TOOLS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "tools.yaml"


def default_registry() -> ToolRegistry:
    """没有配置文件时的兜底（测试、脚本里直接用）。"""
    return load_registry(DEFAULT_TOOLS_PATH)


def load_registry(path: str | Path) -> ToolRegistry:
    """从 config/tools.yaml 装配工具表。

    配置决定：装哪些、叫什么名、什么副作用等级、什么权限、开不开。
    代码只提供 handler 实现。想停用某个工具，改 enabled: false 即可，不用动代码。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"工具配置不存在: {p}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    items = raw.get("tools")
    if not isinstance(items, list):
        raise ValueError(f"{p}: 顶层必须是 tools: [...] 列表")

    reg = ToolRegistry()
    for item in items:
        if not item.get("enabled", True):
            continue
        handler_name = item.get("handler", "")
        handler = HANDLERS.get(handler_name)
        if handler is None:
            raise ValueError(
                f"{p}: 工具 {item.get('name')} 引用了未知 handler '{handler_name}'"
                f"（可用：{', '.join(sorted(HANDLERS))}）"
            )
        reg.register(
            Tool(
                name=item["name"],
                description=item.get("description", ""),
                side_effect=item.get("side_effect", NONE),
                permission=item.get("permission", "L2"),
                handler=handler,
            )
        )
    return reg
