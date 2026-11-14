"""Trace 落库。

要点：trace 失败绝不能拖垮主流程 —— 记录日志本身不能成为新的故障源。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.core.trace import TraceEvent

log = logging.getLogger(__name__)


class PostgresTraceSink:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def write(self, event: TraceEvent) -> None:
        try:
            await self.db.execute(
                """
                INSERT INTO app.trace_event (task_id, step_no, kind, payload, at_ms)
                VALUES ($1, $2, $3, $4, $5)
                """,
                uuid.UUID(event.task_id),
                event.step_no,
                event.kind,
                json.dumps(event.payload, ensure_ascii=False, default=str),
                event.at_ms,
            )
        except Exception as e:
            log.debug("trace 落库失败(kind=%s): %s", event.kind, e)
