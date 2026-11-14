"""MockWorld：冻结时间 + 假日历 + 假记忆 + 可回放的工具。

为什么要 mock 工具：
  1. 可重复——真实邮件发出去了就收不回来，评测不能有副作用
  2. 可断言——工具入参能被精确检查，不用猜
  3. 可对账——"模型说做了" vs "工具真的返回了" 是幻觉率的唯一算法
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from schema import BASE, Case

# 副作用的可撤销程度
NONE = "none"  # 只读
REVERSIBLE = "reversible"  # 可补偿（删日程、删草稿）
EXTERNAL = "external"  # 出圈了，撤不回（已发邮件、已付款）


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    status: str = "ok"  # ok | error
    result: Any = None
    error: str | None = None
    error_code: str | None = None
    side_effect: str = NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": self.args,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "error_code": self.error_code,
            "side_effect": self.side_effect,
        }


@dataclass
class MockWorld:
    case: Case
    now: datetime
    calendar: list[dict[str, Any]] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    wiki: dict[str, str] = field(default_factory=dict)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)

    calls: list[ToolCall] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    _created: int = 0

    # ---------- 构造 ----------
    @classmethod
    def build(cls, case: Case) -> "MockWorld":
        env = case.env
        cal_path = env.resolve(env.calendar)
        mem_path = env.resolve(env.memory)
        tools_path = env.resolve(env.tools)
        wiki_dir = env.resolve(env.wiki)

        calendar = []
        if cal_path and cal_path.exists():
            calendar = yaml.safe_load(cal_path.read_text(encoding="utf-8")).get("events", [])

        memory: dict[str, Any] = {}
        if mem_path and mem_path.exists():
            memory = yaml.safe_load(mem_path.read_text(encoding="utf-8")) or {}

        wiki: dict[str, str] = {}
        if wiki_dir and wiki_dir.exists():
            for p in sorted(wiki_dir.rglob("*.md")):
                wiki[str(p.relative_to(wiki_dir)).replace("\\", "/")] = p.read_text(encoding="utf-8")

        tools = {}
        if tools_path and tools_path.exists():
            tools = json.loads(tools_path.read_text(encoding="utf-8"))

        return cls(case=case, now=case.env.now, calendar=calendar, memory=memory, wiki=wiki, tools=tools)

    # ---------- 快照（用于 environment_unchanged 断言） ----------
    def snapshot(self) -> dict[str, Any]:
        return {
            "calendar": sorted(
                (e["id"], str(e["start"]), str(e["end"])) for e in self.calendar
            ),
            "memory_facts": len(self.memory.get("facts", [])),
        }

    # ---------- 取消 ----------
    def request_cancel(self) -> None:
        self.cancel_requested = True

    # ---------- 工具调用总入口 ----------
    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolCall:
        args = args or {}
        spec = self.tools.get(name)
        if spec is None:
            call = ToolCall(name, args, status="error", error=f"未注册的工具: {name}", error_code="unknown_tool")
            self.calls.append(call)
            return call

        call = ToolCall(name=name, args=args, side_effect=spec.get("side_effect", NONE))
        self.calls.append(call)

        handler = getattr(self, "_t_" + name.replace(".", "_"), None)
        if handler is None:
            call.status = "error"
            call.error = f"工具 {name} 没有 mock 实现"
            call.error_code = "not_implemented"
            return call

        try:
            call.result = handler(args)
        except ToolError as e:
            call.status = "error"
            call.error = e.message
            call.error_code = e.code
        return call

    # ---------- 各工具实现 ----------
    def _t_calendar_list_events(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        lo = _dt(args.get("from"), self.now)
        hi = _dt(args.get("to"), self.now + timedelta(days=7))
        out = []
        for e in self.calendar:
            s = _dt(e["start"], self.now)
            if lo <= s <= hi:
                out.append(e)
        return sorted(out, key=lambda e: e["start"])

    def _t_calendar_create_event(self, args: dict[str, Any]) -> dict[str, Any]:
        for key in ("title", "start", "end"):
            if not args.get(key):
                raise ToolError("bad_args", f"缺少参数 {key}")
        # ID 确定性递增：脚本里要引用它，随机 ID 会让 case 不可复现
        evt = {
            "id": f"evt_{300 + self._created}",
            "title": args["title"],
            "start": args["start"],
            "end": args["end"],
            "attendees": args.get("attendees", []),
        }
        self._created += 1
        self.calendar.append(evt)
        self.effects.append({"kind": "calendar.create", "target": evt["id"]})
        return evt

    def _t_calendar_update_event(self, args: dict[str, Any]) -> dict[str, Any]:
        eid = args.get("event_id")
        evt = next((e for e in self.calendar if e["id"] == eid), None)
        if evt is None:
            raise ToolError("event_not_found", f"日历里没有 id={eid} 的事件")
        for k in ("start", "end", "title"):
            if args.get(k):
                evt[k] = args[k]
        self.effects.append({"kind": "calendar.update", "target": eid})
        return evt

    def _t_calendar_delete_event(self, args: dict[str, Any]) -> dict[str, Any]:
        eid = args.get("event_id")
        evt = next((e for e in self.calendar if e["id"] == eid), None)
        if evt is None:
            raise ToolError("event_not_found", f"日历里没有 id={eid} 的事件")
        self.calendar.remove(evt)
        self.effects.append({"kind": "calendar.delete", "target": eid})
        return {"deleted": eid}

    def _t_mail_draft(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("to"):
            raise ToolError("bad_args", "缺少收件人")
        draft_id = f"draft_{len(self.effects) + 1}"
        self.effects.append({"kind": "mail.draft", "target": draft_id})
        return {"draft_id": draft_id, "status": "draft"}

    def _t_mail_send(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("draft_id"):
            raise ToolError("bad_args", "缺少 draft_id")
        self.effects.append({"kind": "mail.send", "target": args["draft_id"], "irreversible": True})
        return {"sent": True, "draft_id": args["draft_id"], "message_id": "msg_9f2"}

    def _t_mail_delete_draft(self, args: dict[str, Any]) -> dict[str, Any]:
        self.effects.append({"kind": "mail.delete_draft", "target": args.get("draft_id")})
        return {"deleted": args.get("draft_id")}

    def _t_notes_create(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("text"):
            raise ToolError("bad_args", "空笔记")
        self.effects.append({"kind": "notes.create", "target": args["text"][:20]})
        return {"note_id": "note_1"}

    def _t_reminder_create(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args.get("when"):
            raise ToolError("bad_args", "缺少 when")
        self.effects.append({"kind": "reminder.create", "target": args["when"]})
        return {"reminder_id": "rem_1", "when": args["when"]}

    def _t_wiki_search(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        q = args.get("query", "")
        kb = args.get("kb")
        hits = []
        for path, text in self.wiki.items():
            if kb and not path.startswith(str(kb)):
                continue
            score = _overlap(q, text)
            if score > 0:
                hits.append({"path": path, "score": round(score, 3), "snippet": text[:160].replace("\n", " ")})
        hits.sort(key=lambda h: -h["score"])
        return hits[: int(args.get("top_k", 3))]

    def _t_pdf_summarize(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"summary": "（mock）文档要点：编码格式的兼容性取舍，以及作者给出的三条实践建议。"}


# ---------- 小工具 ----------
def _dt(value: Any, default: datetime) -> datetime:
    if value is None:
        return default
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _overlap(query: str, text: str) -> float:
    """中文没空格，用字符二元组重合度做一个够用的朴素召回。"""
    def grams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s)
        return {s[i : i + 2] for i in range(len(s) - 1)}

    qg, tg = grams(query), grams(text)
    if not qg or not tg:
        return 0.0
    return len(qg & tg) / len(qg)


def get_arg(args: dict[str, Any], path: str) -> tuple[bool, Any]:
    """按点号路径取参数，返回 (是否存在, 值)。"""
    cur: Any = args
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False, None
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur
