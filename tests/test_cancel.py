"""取消机制测试：安全点、补偿、级联、真实任务中途取消。"""
import asyncio
import unittest

from app.agent.factory import EchoRuntime
from app.config import AppConfig
from app.core.cancel import SAFE_POINTS, TaskCancelled, TaskRegistry, TaskScope, task_scope
from app.core.runner import TaskConflict, TaskRunner
from app.db.repo import InMemoryTaskRepo


def run(coro):
    return asyncio.run(coro)


class TestSafePoints(unittest.TestCase):
    def test_cancel_before_checkpoint(self):
        async def scenario():
            scope = TaskScope("t1")
            scope.request_cancel("用户点了停止")
            with self.assertRaises(TaskCancelled) as cm:
                await scope.checkpoint("before_tool_call")
            return cm.exception

        exc = run(scenario())
        self.assertEqual(exc.where, "before_tool_call")
        self.assertEqual(exc.reason, "用户点了停止")

    def test_no_cancel_passes(self):
        async def scenario():
            scope = TaskScope("t2")
            await scope.checkpoint("before_model_call")  # 不应抛

        run(scenario())

    def test_unknown_safe_point_rejected(self):
        async def scenario():
            scope = TaskScope("t3")
            with self.assertRaises(ValueError):
                await scope.checkpoint("somewhere_in_the_middle")

        run(scenario())

    def test_all_four_points_exist(self):
        self.assertEqual(len(SAFE_POINTS), 4)


class TestCompensation(unittest.TestCase):
    def test_compensate_reverse_order(self):
        async def scenario():
            scope = TaskScope("t4")
            order = []

            async def first():
                order.append("a")

            async def second():
                order.append("b")

            scope.on_cancel("a", first)
            scope.on_cancel("b", second)
            return await scope.compensate(), order

        result, order = run(scenario())
        self.assertEqual(order, ["b", "a"], "补偿必须逆序")
        self.assertEqual(result, ["ok:b", "ok:a"])

    def test_compensation_failure_is_recorded(self):
        async def scenario():
            scope = TaskScope("t5")

            async def boom():
                raise RuntimeError("撤销失败")

            scope.on_cancel("boom", boom)
            return await scope.compensate()

        result = run(scenario())
        self.assertTrue(result[0].startswith("failed:boom"))


class TestCascade(unittest.TestCase):
    def test_parent_cancel_cascades_to_children(self):
        async def scenario():
            reg = TaskRegistry()
            async with task_scope(reg, "parent"):
                async with task_scope(reg, "child", parent_id="parent"):
                    reg.cancel("parent", "父任务取消")
                    child = reg.get("child")
                    assert child is not None
                    return child.cancelled

        self.assertTrue(run(scenario()))


class TestRealCancellation(unittest.TestCase):
    def _config(self):
        cfg = AppConfig.load()
        cfg.database.dsn = "postgresql://127.0.0.1:1/none"
        cfg.runtime.agent_backend = "echo"
        return cfg

    def test_cancel_running_task_lands_in_cancelled(self):
        async def scenario():
            repo = InMemoryTaskRepo()
            runner = TaskRunner(
                repo, TaskRegistry(), EchoRuntime(delay=0.2), self._config()
            )
            row = await runner.submit(user_input="帮我把这 20 篇论文整理成综述")
            await asyncio.sleep(0.1)
            await runner.cancel(row["id"], "测试取消")

            for _ in range(60):
                task = await repo.get(row["id"])
                if task["state"] in ("cancelled", "failed", "succeeded"):
                    break
                await asyncio.sleep(0.05)
            return await repo.get(row["id"])

        task = run(scenario())
        self.assertEqual(task["state"], "cancelled", f"实际: {task['state']} {task.get('error')}")
        self.assertIsNotNone(task["cancel_requested_at"])

    def test_cancel_finished_task_conflicts(self):
        async def scenario():
            repo = InMemoryTaskRepo()
            runner = TaskRunner(repo, TaskRegistry(), EchoRuntime(delay=0.01), self._config())
            row = await runner.submit(user_input="短任务")
            for _ in range(60):
                task = await repo.get(row["id"])
                if task["state"] == "succeeded":
                    break
                await asyncio.sleep(0.02)
            with self.assertRaises(TaskConflict):
                await runner.cancel(row["id"])

        run(scenario())

    def test_cancel_unknown_task(self):
        async def scenario():
            runner = TaskRunner(
                InMemoryTaskRepo(), TaskRegistry(), EchoRuntime(), self._config()
            )
            with self.assertRaises(KeyError):
                await runner.cancel("not-exists")

        run(scenario())


if __name__ == "__main__":
    unittest.main()
