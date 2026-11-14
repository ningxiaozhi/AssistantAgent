"""取消：三条通道 + 四个安全点 + 副作用补偿。

三条通道各管一件事（架构文档 §5.2）：
    ① DB 标记    —— 该不该停（重启、多进程、事后审计都看得见）
    ② asyncio 事件 —— 多快停（毫秒级，不用轮询）
    ③ 框架中断    —— 停得干不干净（AgentScope 在安全点 unwind）
本文件负责 ②，并暴露 ③ 的挂载点。

关键原则：**不在工具执行中途硬杀**。
asyncio.Task.cancel() 会把协程撕在任意一行，留下写了一半的文件、发了一半的请求。
所以只在这四个位置检查取消：
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable

SAFE_POINTS: tuple[str, ...] = (
    "before_model_call",  # 调模型之前
    "before_tool_call",  # 调工具之前
    "after_tool_call",  # 工具返回之后（此刻可能已有副作用 → 走补偿）
    "before_next_round",  # 每轮 ReAct 结束、进入下一轮之前
)


class TaskCancelled(Exception):
    """在安全点抛出，由 TaskScope 统一收口并执行补偿。"""

    def __init__(self, where: str, reason: str | None = None) -> None:
        super().__init__(f"任务在 {where} 被取消" + (f"：{reason}" if reason else ""))
        self.where = where
        self.reason = reason


@dataclass
class Compensation:
    """一个可逆副作用的撤销动作。"""

    name: str
    action: Callable[[], Awaitable[None]]


@dataclass
class TaskScope:
    task_id: str
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _reason: str | None = None
    _compensations: list[Compensation] = field(default_factory=list)
    _applied: list[str] = field(default_factory=list)
    children: set[str] = field(default_factory=set)

    # ---------- 通道 ② ----------
    def request_cancel(self, reason: str | None = None) -> None:
        self._reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    async def wait_cancelled(self, timeout: float | None = None) -> bool:
        """阻塞等待取消信号（给需要 select 的地方用）。超时返回 False。"""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- 安全点 ----------
    async def checkpoint(self, where: str) -> None:
        if where not in SAFE_POINTS:
            raise ValueError(f"未知安全点: {where}（只允许 {SAFE_POINTS}）")
        # 让出一次事件循环，保证同一 tick 内到达的取消信号也能被看到
        await asyncio.sleep(0)
        if self._event.is_set():
            raise TaskCancelled(where, self._reason)

    # ---------- 副作用补偿 ----------
    def on_cancel(self, name: str, action: Callable[[], Awaitable[None]]) -> None:
        """注册一个补偿动作。只有可撤销的副作用才该注册。"""
        self._compensations.append(Compensation(name, action))

    async def compensate(self) -> list[str]:
        """逆序执行补偿。单个失败不影响其他，全部记录。"""
        done: list[str] = []
        for comp in reversed(self._compensations):
            try:
                await comp.action()
                done.append(f"ok:{comp.name}")
            except Exception as e:  # 补偿失败要留证据，不能静默
                done.append(f"failed:{comp.name}({type(e).__name__}: {e})")
        self._compensations.clear()
        self._applied.extend(done)
        return done

    @property
    def compensation_log(self) -> list[str]:
        return list(self._applied)

    # ---------- 上下文管理 ----------
    async def __aenter__(self) -> "TaskScope":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if isinstance(exc, TaskCancelled):
            await self.compensate()
            return False  # 交给上层记录状态，不吞异常
        return False


class TaskRegistry:
    """进程内所有活跃任务。父任务取消时级联到子任务。"""

    def __init__(self) -> None:
        self._scopes: dict[str, TaskScope] = {}

    def register(self, scope: TaskScope) -> TaskScope:
        self._scopes[scope.task_id] = scope
        return scope

    def get(self, task_id: str) -> TaskScope | None:
        return self._scopes.get(task_id)

    def pop(self, task_id: str) -> TaskScope | None:
        return self._scopes.pop(task_id, None)

    def cancel(self, task_id: str, reason: str | None = None) -> bool:
        """取消一个任务，并级联取消它登记过的子任务。"""
        scope = self._scopes.get(task_id)
        if scope is None:
            return False
        scope.request_cancel(reason)
        for child in scope.children:
            child_scope = self._scopes.get(child)
            if child_scope is not None:
                child_scope.request_cancel(f"父任务 {task_id} 取消")
        return True

    def link_child(self, parent_id: str, child_id: str) -> None:
        parent = self._scopes.get(parent_id)
        if parent is not None:
            parent.children.add(child_id)

    @property
    def active(self) -> list[str]:
        return list(self._scopes)

    def clear(self) -> None:
        self._scopes.clear()


@contextlib.asynccontextmanager
async def task_scope(registry: TaskRegistry, task_id: str, parent_id: str | None = None):
    scope = registry.register(TaskScope(task_id))
    if parent_id:
        registry.link_child(parent_id, task_id)
    try:
        yield scope
    finally:
        registry.pop(task_id)
