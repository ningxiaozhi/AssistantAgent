"""任务执行器：把「提交 → 运行 → 取消 → 补偿 → 落状态」串起来。

这是整个骨架的中枢，也是唯一同时碰三条取消通道的地方：
    ① repo.request_cancel   写 DB
    ② registry.cancel       置 asyncio 事件
    ③ runtime 的 checkpoint 在安全点抛 TaskCancelled
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import AppConfig
from app.core.cancel import TaskCancelled, TaskRegistry, TaskScope
from app.core.tasks import (
    TaskState,
    is_cancellable,
    require_transition,
)
from app.core.trace import Trace, TraceSink

log = logging.getLogger(__name__)


class TaskConflict(Exception):
    """不可取消 / 状态已变。API 层映射成 409。"""


class TaskRunner:
    def __init__(
        self,
        repo: Any,
        registry: TaskRegistry,
        runtime: Any,
        config: AppConfig,
        sink: TraceSink | None = None,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.runtime = runtime
        self.config = config
        self.sink = sink
        self._jobs: dict[str, asyncio.Task[Any]] = {}

    # ---------------------------------------------------------------- 提交
    async def submit(
        self,
        *,
        user_input: str,
        session_id: str | None = None,
        channel: str | None = None,
        model_version: str | None = None,
        sensitive: bool = False,
    ) -> dict[str, Any]:
        row = await self.repo.create(
            input=user_input,
            session_id=session_id,
            channel=channel,
            model_version=model_version,
        )
        task_id = row["id"]
        job = asyncio.create_task(self._execute(task_id, user_input, sensitive))
        self._jobs[task_id] = job
        job.add_done_callback(lambda _t, tid=task_id: self._jobs.pop(tid, None))
        return row

    # ---------------------------------------------------------------- 执行
    async def _execute(self, task_id: str, user_input: str, sensitive: bool) -> None:
        scope: TaskScope = self.registry.register(TaskScope(task_id))
        trace = Trace(task_id, sink=self.sink)

        try:
            try:
                await self.repo.transition(task_id, TaskState.RUNNING)
            except Exception as e:
                # 提交的瞬间就被取消 / 版本冲突：不跑，直接退出
                await trace.record("task.skip", reason=str(e))
                return

            await trace.record("task.start", input=user_input, runtime=self.runtime.name)
            output = await self.runtime.run(
                task_id=task_id,
                user_input=user_input,
                scope=scope,
                trace=trace,
                sensitive=sensitive,
            )
            await self.repo.transition(
                task_id, TaskState.SUCCEEDED, output=output, steps=trace.step_no
            )
            await trace.record("task.end", state=TaskState.SUCCEEDED.value)

        except TaskCancelled as e:
            log.info("任务 %s 在 %s 被取消", task_id, e.where)
            await scope.compensate()
            await trace.record("task.cancelled", where=e.where, reason=e.reason)
            await self._finish_cancelled(task_id, str(e))

        except Exception as e:  # 任何异常都要落状态，不能留 running 僵尸
            log.exception("任务 %s 失败", task_id)
            await trace.record("task.error", error=f"{type(e).__name__}: {e}")
            await self._safe_transition(task_id, TaskState.FAILED, error=f"{type(e).__name__}: {e}")

        finally:
            self.registry.pop(task_id)

    async def _finish_cancelled(self, task_id: str, reason: str) -> None:
        task = await self.repo.get(task_id)
        if task is None:
            return
        state = TaskState(task["state"])
        try:
            if state is not TaskState.CANCELLING:
                require_transition(state, TaskState.CANCELLING)
                await self.repo.transition(task_id, TaskState.CANCELLING, cancel_reason=reason)
            await self.repo.transition(task_id, TaskState.CANCELLED, error=reason)
        except Exception as e:
            log.warning("收尾取消状态失败 %s: %s", task_id, e)

    async def _safe_transition(self, task_id: str, state: TaskState, **fields: Any) -> None:
        try:
            await self.repo.transition(task_id, state, **fields)
        except Exception as e:
            log.warning("落状态 %s -> %s 失败: %s", task_id, state, e)

    # ---------------------------------------------------------------- 取消
    async def cancel(self, task_id: str, reason: str | None = None) -> dict[str, Any]:
        task = await self.repo.get(task_id)
        if task is None:
            raise KeyError(task_id)

        state = TaskState(task["state"])
        if not is_cancellable(state):
            raise TaskConflict(f"任务已处于 {state.value}，不可取消")

        row = await self.repo.request_cancel(task_id, reason)  # ① DB
        if row is None:
            raise TaskConflict("任务刚好结束，取消未生效")

        self.registry.cancel(task_id, reason)  # ② 进程内信号 → ③ 安全点

        if state is TaskState.QUEUED:
            await self._safe_transition(task_id, TaskState.CANCELLED, cancel_reason=reason)
        else:
            await self._safe_transition(task_id, TaskState.CANCELLING, cancel_reason=reason)

        return await self.repo.get(task_id) or row

    # ---------------------------------------------------------------- 其它
    async def shutdown(self, grace: float = 3.0) -> None:
        """停机：给在跑的任务一个取消窗口，超时就放掉，避免"重启后还在跑"。"""
        for task_id in list(self._jobs):
            self.registry.cancel(task_id, "服务停机")
        if self._jobs:
            await asyncio.wait(list(self._jobs.values()), timeout=grace)

    @property
    def running(self) -> list[str]:
        return list(self._jobs)
