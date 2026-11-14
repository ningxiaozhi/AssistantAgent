"""任务状态机。

为什么把状态机单独抽出来：
  个人助理最容易出的事故不是"算错"，是"状态说不清"——
  用户按了取消，界面上显示已取消，但后台还在跑，或者已经产生了副作用。
  所以状态与合法迁移必须是**穷举的、可测的**，而不是散在 if-else 里。

  刻意保留两个中间态：
    cancelling  已收到取消、尚未到安全点 —— 没有它，就分不清"要停"和"已停"
    interrupted 非用户意图的暂停（进程重启）—— 可以 resume，不算失败
"""
from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_CONFIRM = "waiting_confirm"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


TERMINAL: frozenset[TaskState] = frozenset(
    {TaskState.CANCELLED, TaskState.SUCCEEDED, TaskState.FAILED}
)

# 穷举所有合法迁移。不在表里的，一律拒绝。
ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.WAITING_CONFIRM,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLING,
            TaskState.INTERRUPTED,
        }
    ),
    TaskState.WAITING_CONFIRM: frozenset(
        {
            TaskState.RUNNING,
            TaskState.CANCELLING,
            TaskState.CANCELLED,
            TaskState.FAILED,
            TaskState.INTERRUPTED,  # 进程重启时，等人确认的任务只能算中断
        }
    ),
    TaskState.CANCELLING: frozenset({TaskState.CANCELLED}),
    TaskState.INTERRUPTED: frozenset({TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.CANCELLED: frozenset(),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, src: TaskState, dst: TaskState) -> None:
        super().__init__(f"非法状态迁移: {src} -> {dst}")
        self.src, self.dst = src, dst


def can_transition(src: TaskState, dst: TaskState) -> bool:
    return dst in ALLOWED.get(src, frozenset())


def require_transition(src: TaskState, dst: TaskState) -> None:
    if not can_transition(src, dst):
        raise InvalidTransition(TaskState(src), TaskState(dst))


def is_cancellable(state: TaskState) -> bool:
    """只有这三种状态能接受取消请求。已结束的任务再取消，语义上是错的。"""
    return state in (TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING_CONFIRM)


def next_after_cancel_request(state: TaskState) -> TaskState:
    """收到取消请求后应该进入的状态。

    queued 还没开始跑，可以直接终结；running / waiting_confirm 得走到安全点。
    """
    if state is TaskState.QUEUED:
        return TaskState.CANCELLED
    return TaskState.CANCELLING
