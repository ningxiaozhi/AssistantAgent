"""任务仓储：两个实现，同一套接口。

InMemory —— 单测与"没有数据库也能跑"的降级模式
Postgres —— 线上用，带乐观锁

乐观锁是取消/确认竞态的唯一正确答案：
两个人（用户点确认 vs 用户点取消）同时写一行，谁先到谁赢，后到的拿到冲突。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.tasks import TaskState

TASK_COLUMNS = (
    "id",
    "session_id",
    "channel",
    "input",
    "state",
    "version",
    "parent_id",
    "cancel_requested_at",
    "cancel_reason",
    "model_version",
    "cost_usd",
    "steps",
    "output",
    "error",
    "created_at",
    "updated_at",
)

MUTABLE_FIELDS = {"output", "error", "steps", "cost_usd", "cancel_reason", "model_version"}


class VersionConflict(Exception):
    """别人先改了这行。调用方应重新读一次再决定。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryTaskRepo:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.tool_calls: list[dict[str, Any]] = []
        self.permissions: list[dict[str, Any]] = []

    async def create(
        self,
        *,
        input: str,
        session_id: str | None = None,
        channel: str | None = None,
        model_version: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        tid = uuid.uuid4().hex
        row = {
            "id": tid,
            "session_id": session_id,
            "channel": channel,
            "input": input,
            "state": TaskState.QUEUED.value,
            "version": 1,
            "parent_id": parent_id,
            "cancel_requested_at": None,
            "cancel_reason": None,
            "model_version": model_version,
            "cost_usd": 0.0,
            "steps": 0,
            "output": None,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.tasks[tid] = row
        return dict(row)

    async def get(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return dict(row) if row else None

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = sorted(self.tasks.values(), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def transition(
        self,
        task_id: str,
        to_state: TaskState,
        *,
        expect_version: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        row = self.tasks.get(task_id)
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        if expect_version is not None and row["version"] != expect_version:
            raise VersionConflict(f"版本冲突：期望 {expect_version}，实际 {row['version']}")
        bad = set(fields) - MUTABLE_FIELDS
        if bad:
            raise ValueError(f"不可写字段: {bad}")
        row.update({k: v for k, v in fields.items() if k in MUTABLE_FIELDS})
        row["state"] = TaskState(to_state).value
        row["version"] += 1
        row["updated_at"] = _now()
        return dict(row)

    async def request_cancel(self, task_id: str, reason: str | None = None) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        if row is None or row["state"] not in {"queued", "running", "waiting_confirm"}:
            return None
        row["cancel_requested_at"] = _now()
        row["cancel_reason"] = reason
        row["version"] += 1
        row["updated_at"] = _now()
        return dict(row)

    async def record_tool_call(self, task_id: str, **fields: Any) -> None:
        self.tool_calls.append({"task_id": task_id, **fields})

    async def record_permission(
        self, task_id: str, tool: str, decision: str, rule: str | None = None
    ) -> None:
        self.permissions.append(
            {
                "task_id": task_id,
                "tool": tool,
                "decision": decision,
                "rule": rule,
                "decided_by": "policy" if rule in ("timeout", "cancel") else "user",
            }
        )

    async def list_active(self) -> list[dict[str, Any]]:
        active = {"running", "waiting_confirm", "cancelling"}
        return [dict(r) for r in self.tasks.values() if r["state"] in active]


class PostgresTaskRepo:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def create(
        self,
        *,
        input: str,
        session_id: str | None = None,
        channel: str | None = None,
        model_version: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        row = await self.db.fetchrow(
            """
            INSERT INTO app.task (id, session_id, channel, input, model_version, parent_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            uuid.uuid4(),
            session_id,
            channel,
            input,
            model_version,
            uuid.UUID(parent_id) if parent_id else None,
        )
        return dict(row)

    async def get(self, task_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchrow("SELECT * FROM app.task WHERE id = $1", uuid.UUID(task_id))
        return dict(row) if row else None

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT * FROM app.task ORDER BY created_at DESC LIMIT $1", limit
        )
        return [dict(r) for r in rows]

    async def transition(
        self,
        task_id: str,
        to_state: TaskState,
        *,
        expect_version: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        bad = set(fields) - MUTABLE_FIELDS
        if bad:
            raise ValueError(f"不可写字段: {bad}")

        sets = ["state = $2", "version = version + 1", "updated_at = now()"]
        values: list[Any] = [uuid.UUID(task_id), TaskState(to_state).value]
        for key, value in fields.items():
            values.append(value)
            sets.append(f"{key} = ${len(values)}")

        sql = f"UPDATE app.task SET {', '.join(sets)} WHERE id = $1"
        if expect_version is not None:
            values.append(expect_version)
            sql += f" AND version = ${len(values)}"
        sql += " RETURNING *"

        row = await self.db.fetchrow(sql, *values)
        if row is None:
            raise VersionConflict(f"任务 {task_id} 版本冲突或不存在")
        return dict(row)

    async def request_cancel(self, task_id: str, reason: str | None = None) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            UPDATE app.task
               SET cancel_requested_at = now(),
                   cancel_reason = $2,
                   version = version + 1,
                   updated_at = now()
             WHERE id = $1 AND state IN ('queued', 'running', 'waiting_confirm')
            RETURNING *
            """,
            uuid.UUID(task_id),
            reason,
        )
        return dict(row) if row else None

    async def record_tool_call(self, task_id: str, **fields: Any) -> None:
        await self.db.execute(
            """
            INSERT INTO app.tool_call
                (task_id, step_no, tool, args, status, error_code, side_effect, idempotency_key, latency_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            uuid.UUID(task_id),
            fields.get("step_no", 0),
            fields.get("tool", ""),
            json.dumps(fields.get("args", {}), ensure_ascii=False),
            fields.get("status", "ok"),
            fields.get("error_code"),
            fields.get("side_effect", "none"),
            fields.get("idempotency_key"),
            fields.get("latency_ms"),
        )

    async def record_permission(
        self, task_id: str, tool: str, decision: str, rule: str | None = None
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO app.permission_event (task_id, tool, decision, rule, decided_by)
            VALUES ($1, $2, $3, $4, $5)
            """,
            uuid.UUID(task_id),
            tool,
            decision,
            rule,
            "policy" if rule in ("timeout", "cancel") else "user",
        )

    async def list_active(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            "SELECT * FROM app.task WHERE state IN ('running', 'waiting_confirm', 'cancelling')"
        )
        return [dict(r) for r in rows]
