"""工具实现层：只提供 handler 与统一的副作用契约。

**注册与治理不在这里**，在 `toolbox.py` 和 `config/tools.yaml`：
    库怎么分、哪个角色能看哪些库、权限封顶多少 —— 全是配置，不是代码。

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

NONE = "none"
REVERSIBLE = "reversible"
EXTERNAL = "external"

DATA_DIR = Path("data")


# ---------------------------------------------------------------- 数据结构
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
    library: str = ""  # 归属的库，由 toolbox 在装配时填入
    idempotency: str = "{task_id}:{tool}:{hash}"

    def key(self, task_id: str, args: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(args, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:12]
        return self.idempotency.format(task_id=task_id, tool=self.name, hash=digest)


class ToolRegistry:
    """一次任务实际可用的工具集合（平铺视图，由某个 library 子集装配而来）。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.side_effect not in (NONE, REVERSIBLE, EXTERNAL):
            raise ValueError(f"{tool.name}: 非法 side_effect {tool.side_effect}")
        if tool.permission not in ("L0", "L1", "L2", "L3"):
            raise ValueError(f"{tool.name}: 非法权限等级 {tool.permission}")
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, Any]]:
        """给模型看的工具清单（不含实现）。只包含本次任务选中的库里的工具。"""
        return [
            {
                "name": t.name,
                "library": t.library,
                "description": t.description,
                "side_effect": t.side_effect,
                "permission": t.permission,
            }
            for t in self._tools.values()
        ]

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools.values())


# ---------------------------------------------------------------- 存储小工具
def _path(name: str) -> Path:
    DATA_DIR.mkdir(exist_ok=True)
    return DATA_DIR / name


def _append(name: str, record: dict[str, Any]) -> None:
    with _path(name).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_lines(name: str) -> list[dict[str, Any]]:
    p = _path(name)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bad_arg(field: str) -> ToolResult:
    return ToolResult(ok=False, error=f"缺少参数 {field}", error_code="bad_args")


# ---------------------------------------------------------------- 日程
async def calendar_list_events(args: dict[str, Any]) -> ToolResult:
    """ISO 字符串按字典序比较即可（同格式同时区）。"""
    lo = args.get("from") or "0000"
    hi = args.get("to") or "9999"
    events = [e for e in _read_lines("calendar.jsonl") if lo <= e.get("start", "")[: len(lo)] <= hi]
    return ToolResult(ok=True, data={"events": events}, side_effect=NONE)


async def calendar_create_event(args: dict[str, Any]) -> ToolResult:
    for field in ("title", "start", "end"):
        if not args.get(field):
            return _bad_arg(field)
    evt = {
        "id": f"evt_{uuid.uuid4().hex[:8]}",
        "title": args["title"],
        "start": args["start"],
        "end": args["end"],
        "attendees": args.get("attendees", []),
    }
    _append("calendar.jsonl", evt)
    return ToolResult(ok=True, data=evt, side_effect=REVERSIBLE)


# ---------------------------------------------------------------- 邮件
async def mail_draft(args: dict[str, Any]) -> ToolResult:
    if not args.get("to"):
        return _bad_arg("to")
    draft = {
        "draft_id": f"draft_{uuid.uuid4().hex[:8]}",
        "to": args["to"],
        "subject": args.get("subject", ""),
        "body": args.get("body", ""),
        "at": _now_iso(),
    }
    _append("mail_drafts.jsonl", draft)
    return ToolResult(ok=True, data=draft, side_effect=REVERSIBLE)


# ---------------------------------------------------------------- 笔记
async def notes_create(args: dict[str, Any]) -> ToolResult:
    text = (args.get("text") or "").strip()
    if not text:
        return _bad_arg("text")
    record = {"id": uuid.uuid4().hex[:8], "text": text, "tag": args.get("tag", "inbox"), "at": _now_iso()}
    _append("notes.jsonl", record)
    return ToolResult(ok=True, data=record, side_effect=REVERSIBLE)


async def notes_search(args: dict[str, Any]) -> ToolResult:
    q = (args.get("q") or "").strip()
    if not q:
        return _bad_arg("q")
    hits = [n for n in _read_lines("notes.jsonl") if q in n.get("text", "")]
    return ToolResult(ok=True, data={"hits": hits[: int(args.get("limit", 5))]}, side_effect=NONE)


# ---------------------------------------------------------------- 待办
async def task_add(args: dict[str, Any]) -> ToolResult:
    text = (args.get("text") or "").strip()
    if not text:
        return _bad_arg("text")
    item = {"id": uuid.uuid4().hex[:8], "text": text, "when": args.get("when"), "at": _now_iso()}
    _append("tasks.jsonl", item)
    return ToolResult(ok=True, data=item, side_effect=REVERSIBLE)


# ---------------------------------------------------------------- 检索与文档
async def web_search(args: dict[str, Any]) -> ToolResult:
    q = (args.get("query") or "").strip()
    if not q:
        return _bad_arg("query")
    return ToolResult(
        ok=True,
        data={
            "query": q,
            "results": [],
            "note": "占位实现：接真实搜索时替换这里，接口形状保持不变",
        },
        side_effect=NONE,
    )


async def doc_summarize(args: dict[str, Any]) -> ToolResult:
    path = args.get("path")
    if not path:
        return _bad_arg("path")
    return ToolResult(
        ok=True,
        data={"path": path, "summary": "占位实现：接解析器后返回真实摘要", "chars": 0},
        side_effect=NONE,
    )


# ---------------------------------------------------------------- 生活
async def life_record_expense(args: dict[str, Any]) -> ToolResult:
    amount = args.get("amount")
    if amount is None:
        return _bad_arg("amount")
    item = {
        "id": uuid.uuid4().hex[:8],
        "amount": amount,
        "category": args.get("category", "其他"),
        "note": args.get("note", ""),
        "at": _now_iso(),
    }
    _append("ledger.jsonl", item)
    return ToolResult(ok=True, data=item, side_effect=REVERSIBLE)


# ---------------------------------------------------------------- 系统
async def time_now(args: dict[str, Any]) -> ToolResult:
    return ToolResult(ok=True, data={"now": _now_iso()}, side_effect=NONE)


async def system_info(args: dict[str, Any]) -> ToolResult:
    return ToolResult(
        ok=True,
        data={"app": "AssistantAgent", "version": "0.1.0", "tools_loaded_from": "config/tools.yaml"},
        side_effect=NONE,
    )


# handler 名 → 实现。config/tools.yaml 里的 handler 字段指向这里。
HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[ToolResult]]] = {
    "calendar_list_events": calendar_list_events,
    "calendar_create_event": calendar_create_event,
    "mail_draft": mail_draft,
    "notes_create": notes_create,
    "notes_search": notes_search,
    "task_add": task_add,
    "web_search": web_search,
    "doc_summarize": doc_summarize,
    "life_record_expense": life_record_expense,
    "time_now": time_now,
    "system_info": system_info,
}
