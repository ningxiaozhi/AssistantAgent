"""确认闸门（HITL）：L2 动作在真正执行前停下来问一句。

难点不是"发个通知"，是**暂停之后怎么恢复**。三种选择：

  ① 进程内 future  —— 任务协程 `await` 一个 future，用户点确认就 set_result
  ② 断点持久化     —— 把状态存下来，重启后从断点重放
  ③ 干脆不暂停     —— 先执行再告知（不要这么做）

这里实现 ①。代价说清楚：**进程重启时，正在等待确认的任务会丢**，
所以启动时会把遗留的 running/waiting_confirm 标成 `interrupted`，而不是假装它还在。
② 是后续的事，需要 AgentScope 的状态快照（`AgentState`）配合。

等待期间必须同时盯两件事：用户答复、任务被取消。少盯一个就会出现
"点了取消，任务还在等你确认"这种僵尸。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.cancel import TaskCancelled, TaskScope
from app.core.tasks import TaskState, can_transition, is_cancellable

log = logging.getLogger(__name__)


class ConfirmationRejected(Exception):
    """用户明确拒绝。"""

    def __init__(self, tool: str) -> None:
        super().__init__(f"用户拒绝执行 {tool}")
        self.tool = tool


class ConfirmationExpired(Exception):
    """超时未确认。"""

    def __init__(self, tool: str, timeout_s: float) -> None:
        super().__init__(f"{tool} 等待确认超过 {timeout_s:.0f}s")
        self.tool = tool


@dataclass
class ConfirmationRequest:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    permission: str = "L2"
    reason: str = ""
    preview: str = ""


@dataclass
class Pending:
    task_id: str
    request: ConfirmationRequest
    future: asyncio.Future[bool]
    created_at: float
    timeout_s: float

    @property
    def expires_in(self) -> float:
        return max(0.0, self.created_at + self.timeout_s - time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "tool": self.request.tool,
            "args": self.request.args,
            "permission": self.request.permission,
            "reason": self.request.reason,
            "preview": self.request.preview,
            "expires_in_s": round(self.expires_in, 1),
        }


class PermissionGate:
    def __init__(self, repo: Any, timeout_s: float = 300.0) -> None:
        self.repo = repo
        self.timeout_s = timeout_s
        self._pending: dict[str, Pending] = {}

    # ---------------------------------------------------------------- 执行侧
    async def ask(
        self,
        task_id: str,
        request: ConfirmationRequest,
        scope: TaskScope,
    ) -> None:
        """阻塞到用户答复。拒绝/超时/取消都抛异常，绝不"默默继续"。"""
        task = await self.repo.get(task_id)
        if task is None:
            raise TaskCancelled("waiting_confirm", "任务不存在")
        if not can_transition(TaskState(task["state"]), TaskState.WAITING_CONFIRM):
            # 已经被取消或已结束，别再挂起
            raise TaskCancelled("before_confirm", f"任务处于 {task['state']}")

        loop = asyncio.get_running_loop()
        pending = Pending(
            task_id=task_id,
            request=request,
            future=loop.create_future(),
            created_at=time.time(),
            timeout_s=self.timeout_s,
        )
        self._pending[task_id] = pending
        await self.repo.transition(task_id, TaskState.WAITING_CONFIRM)
        await self.repo.record_permission(task_id, request.tool, "ask", request.permission)

        cancelled_waiter = asyncio.ensure_future(scope.wait_cancelled(self.timeout_s))
        try:
            done, _ = await asyncio.wait(
                {pending.future, cancelled_waiter}, return_when=asyncio.FIRST_COMPLETED
            )

            if pending.future in done:
                approved = bool(pending.future.result())
                if not approved:
                    await self.repo.record_permission(task_id, request.tool, "denied", "user")
                    raise ConfirmationRejected(request.tool)
                await self.repo.record_permission(task_id, request.tool, "allowed", "user")
                await self._resume(task_id)
                return

            # 取消信号先到：区分"用户取消"和"等超时了"（两者都用同一个 waiter）
            if cancelled_waiter.result():
                await self.repo.record_permission(task_id, request.tool, "denied", "cancel")
                raise TaskCancelled("waiting_confirm", "等待确认时被取消")

            await self.repo.record_permission(task_id, request.tool, "denied", "timeout")
            raise ConfirmationExpired(request.tool, self.timeout_s)

        finally:
            self._pending.pop(task_id, None)
            if not cancelled_waiter.done():
                cancelled_waiter.cancel()

    async def _resume(self, task_id: str) -> None:
        task = await self.repo.get(task_id)
        if task is None:
            return
        state = TaskState(task["state"])
        if state is TaskState.WAITING_CONFIRM:
            await self.repo.transition(task_id, TaskState.RUNNING)

    # ---------------------------------------------------------------- 接口侧
    def pending(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self._pending.values()]

    def get(self, task_id: str) -> Pending | None:
        return self._pending.get(task_id)

    def resolve(self, task_id: str, approved: bool) -> bool:
        """用户点了确认/拒绝。返回是否真的找到了等待中的请求。"""
        pending = self._pending.get(task_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(approved)
        return True

    def count(self) -> int:
        return len(self._pending)


async def recover_stale_tasks(repo: Any) -> int:
    """启动清场：上次进程留下的 running / waiting_confirm 一律标 interrupted。

    不这么做，任务会永远卡在"运行中"，而实际上没有任何协程在跑它。
    """
    if not hasattr(repo, "list_active"):
        return 0
    stale = await repo.list_active()
    fixed = 0
    for task in stale:
        state = TaskState(task["state"])
        if state in (TaskState.CANCELLING,):
            target = TaskState.CANCELLED
        elif state in (TaskState.RUNNING, TaskState.WAITING_CONFIRM):
            target = TaskState.INTERRUPTED
        else:
            continue
        try:
            await repo.transition(task["id"], target, error="进程重启，任务已中断")
            fixed += 1
        except Exception as e:  # 单个失败不阻塞启动
            log.warning("清理遗留任务 %s 失败: %s", task["id"], e)
    if fixed:
        log.warning("启动清理：%d 个遗留任务已标记为 interrupted/cancelled", fixed)
    return fixed


def may_cancel(task: dict[str, Any]) -> bool:
    return is_cancellable(TaskState(task["state"]))
