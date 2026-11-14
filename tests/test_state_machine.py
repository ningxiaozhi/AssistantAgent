"""状态机测试：所有合法迁移必须穷举，非法迁移必须报错。

这是最早该写的一批测试 —— 取消/确认的竞态，本质上就是状态说错了。
"""
import unittest

from app.core.tasks import (
    ALLOWED,
    TERMINAL,
    InvalidTransition,
    TaskState,
    can_transition,
    is_cancellable,
    next_after_cancel_request,
    require_transition,
)


class TestTransitions(unittest.TestCase):
    def test_happy_path(self):
        for src, dst in [
            (TaskState.QUEUED, TaskState.RUNNING),
            (TaskState.RUNNING, TaskState.SUCCEEDED),
            (TaskState.RUNNING, TaskState.WAITING_CONFIRM),
            (TaskState.WAITING_CONFIRM, TaskState.RUNNING),
        ]:
            self.assertTrue(can_transition(src, dst), f"{src} -> {dst} 应该允许")

    def test_cancel_path(self):
        self.assertTrue(can_transition(TaskState.RUNNING, TaskState.CANCELLING))
        self.assertTrue(can_transition(TaskState.CANCELLING, TaskState.CANCELLED))
        # 不允许跳过 cancelling 直接 cancelled —— 那就分不清"要停"和"已停"
        self.assertFalse(can_transition(TaskState.RUNNING, TaskState.CANCELLED))

    def test_terminal_states_are_dead_ends(self):
        for state in TERMINAL:
            self.assertEqual(ALLOWED[state], frozenset(), f"{state} 不应再有后续迁移")
            self.assertFalse(can_transition(state, TaskState.RUNNING))

    def test_illegal_transition_raises(self):
        with self.assertRaises(InvalidTransition):
            require_transition(TaskState.SUCCEEDED, TaskState.RUNNING)

    def test_resume_from_interrupted(self):
        self.assertTrue(can_transition(TaskState.INTERRUPTED, TaskState.RUNNING))


class TestCancelRules(unittest.TestCase):
    def test_only_active_states_cancellable(self):
        self.assertTrue(is_cancellable(TaskState.QUEUED))
        self.assertTrue(is_cancellable(TaskState.RUNNING))
        self.assertTrue(is_cancellable(TaskState.WAITING_CONFIRM))
        self.assertFalse(is_cancellable(TaskState.SUCCEEDED))
        self.assertFalse(is_cancellable(TaskState.CANCELLED))
        self.assertFalse(is_cancellable(TaskState.CANCELLING))

    def test_queued_cancel_skips_cancelling(self):
        self.assertIs(next_after_cancel_request(TaskState.QUEUED), TaskState.CANCELLED)
        self.assertIs(next_after_cancel_request(TaskState.RUNNING), TaskState.CANCELLING)


if __name__ == "__main__":
    unittest.main()
