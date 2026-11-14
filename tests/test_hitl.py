"""HITL 确认闸门测试：同意 / 拒绝 / 超时 / 取消竞速 / 重启清场。

这一层最容易出的问题是"僵尸"：任务卡在 waiting_confirm，界面上还显示运行中，
或者点了取消但闸门还在傻等。所以每个分支都要单独钉一遍。
"""
import asyncio
import time
import unittest

from app.agent.factory import EchoRuntime
from app.config import AppConfig
from app.core.cancel import TaskCancelled, TaskRegistry, TaskScope
from app.core.confirm import (
    ConfirmationExpired,
    ConfirmationRejected,
    ConfirmationRequest,
    PermissionGate,
    recover_stale_tasks,
)
from app.core.runner import TaskRunner
from app.core.tasks import TaskState
from app.db.repo import InMemoryTaskRepo


def run(coro):
    return asyncio.run(coro)


def _config() -> AppConfig:
    cfg = AppConfig.load()
    cfg.database.dsn = "postgresql://127.0.0.1:1/none"
    cfg.runtime.agent_backend = "echo"
    return cfg


class ConfirmingRuntime:
    """只做一件事：请求确认。"""

    name = "confirming"

    def __init__(self, gate: PermissionGate, tool: str = "life.record_expense") -> None:
        self.gate = gate
        self.tool = tool

    async def run(self, *, task_id, user_input, scope, trace, sensitive=False):
        await self.gate.ask(
            task_id,
            ConfirmationRequest(tool=self.tool, args={"amount": 38}, reason="涉及金额"),
            scope,
        )
        return "已执行"


async def _wait_state(repo, task_id, states, timeout=3.0):
    deadline = time.time() + timeout
    task = {}
    while time.time() < deadline:
        task = await repo.get(task_id)
        if task and task["state"] in states:
            return task
        await asyncio.sleep(0.02)
    return task


class TestGate(unittest.TestCase):
    def _setup(self, timeout_s: float = 5.0):
        repo = InMemoryTaskRepo()
        gate = PermissionGate(repo, timeout_s=timeout_s)
        return repo, gate

    def test_approve_resumes(self):
        async def scenario():
            repo, gate = self._setup()
            task = await repo.create(input="记账")
            await repo.transition(task["id"], TaskState.RUNNING)
            scope = TaskScope(task["id"])

            async def answer():
                await asyncio.sleep(0.05)
                # 此刻任务应该已经挂起
                assert await repo.get(task["id"]) is not None
                self.assertTrue(gate.resolve(task["id"], True))

            waiter = asyncio.ensure_future(answer())
            await gate.ask(
                task["id"], ConfirmationRequest(tool="life.record_expense"), scope
            )
            await waiter
            return await repo.get(task["id"]), repo.permissions

        task, perms = run(scenario())
        self.assertEqual(task["state"], "running", "同意后要回到 running")
        self.assertTrue(any(p["decision"] == "ask" for p in perms))
        self.assertTrue(any(p["decision"] == "allowed" for p in perms))

    def test_reject_raises(self):
        async def scenario():
            repo, gate = self._setup()
            task = await repo.create(input="记账")
            await repo.transition(task["id"], TaskState.RUNNING)
            scope = TaskScope(task["id"])

            async def answer():
                await asyncio.sleep(0.05)
                gate.resolve(task["id"], False)

            waiter = asyncio.ensure_future(answer())
            with self.assertRaises(ConfirmationRejected):
                await gate.ask(task["id"], ConfirmationRequest(tool="x"), scope)
            await waiter
            return repo.permissions

        perms = run(scenario())
        self.assertTrue(any(p["decision"] == "denied" for p in perms))

    def test_timeout_raises(self):
        async def scenario():
            repo, gate = self._setup(timeout_s=0.15)
            task = await repo.create(input="记账")
            await repo.transition(task["id"], TaskState.RUNNING)
            with self.assertRaises(ConfirmationExpired):
                await gate.ask(task["id"], ConfirmationRequest(tool="x"), TaskScope(task["id"]))

        run(scenario())

    def test_cancel_wins_over_waiting(self):
        async def scenario():
            repo, gate = self._setup(timeout_s=5)
            task = await repo.create(input="记账")
            await repo.transition(task["id"], TaskState.RUNNING)
            scope = TaskScope(task["id"])

            async def cancel_soon():
                await asyncio.sleep(0.05)
                scope.request_cancel("用户点了停止")

            waiter = asyncio.ensure_future(cancel_soon())
            with self.assertRaises(TaskCancelled):
                await gate.ask(task["id"], ConfirmationRequest(tool="x"), scope)
            await waiter

        run(scenario())

    def test_resolve_when_not_pending_returns_false(self):
        repo, gate = self._setup()
        self.assertFalse(gate.resolve("nope", True))

    def test_refuses_to_park_finished_task(self):
        async def scenario():
            repo, gate = self._setup()
            task = await repo.create(input="x")
            await repo.transition(task["id"], TaskState.SUCCEEDED)
            with self.assertRaises(TaskCancelled):
                await gate.ask(task["id"], ConfirmationRequest(tool="x"), TaskScope(task["id"]))

        run(scenario())


class TestRunnerIntegration(unittest.TestCase):
    def _runner(self, timeout_s: float = 5.0):
        repo = InMemoryTaskRepo()
        gate = PermissionGate(repo, timeout_s=timeout_s)
        runtime = ConfirmingRuntime(gate)
        return repo, gate, TaskRunner(repo, TaskRegistry(), runtime, _config())

    def test_confirm_then_succeed(self):
        async def scenario():
            repo, gate, runner = self._runner()
            task = await runner.submit(user_input="记一笔 38 块打车")
            parked = await _wait_state(repo, task["id"], {"waiting_confirm"})
            self.assertEqual(parked["state"], "waiting_confirm")

            self.assertTrue(gate.resolve(task["id"], True))
            done = await _wait_state(repo, task["id"], {"succeeded", "failed", "cancelled"})
            return done

        task = run(scenario())
        self.assertEqual(task["state"], "succeeded", task.get("error"))

    def test_reject_then_cancelled(self):
        async def scenario():
            repo, gate, runner = self._runner()
            task = await runner.submit(user_input="记一笔 38 块打车")
            await _wait_state(repo, task["id"], {"waiting_confirm"})
            gate.resolve(task["id"], False)
            return await _wait_state(repo, task["id"], {"succeeded", "failed", "cancelled"})

        task = run(scenario())
        self.assertEqual(task["state"], "cancelled")
        self.assertIn("拒绝", task["error"])

    def test_timeout_then_cancelled(self):
        async def scenario():
            repo, gate, runner = self._runner(timeout_s=0.15)
            task = await runner.submit(user_input="记一笔 38 块打车")
            return await _wait_state(repo, task["id"], {"succeeded", "failed", "cancelled"}, 3.0)

        task = run(scenario())
        self.assertEqual(task["state"], "cancelled")
        self.assertIn("确认", task["error"])

    def test_cancel_while_waiting(self):
        async def scenario():
            repo, gate, runner = self._runner()
            task = await runner.submit(user_input="记一笔 38 块打车")
            await _wait_state(repo, task["id"], {"waiting_confirm"})
            await runner.cancel(task["id"], "不记了")
            return await _wait_state(repo, task["id"], {"cancelled", "failed"})

        task = run(scenario())
        self.assertEqual(task["state"], "cancelled")


class TestRecovery(unittest.TestCase):
    def test_stale_tasks_are_marked_interrupted(self):
        async def scenario():
            repo = InMemoryTaskRepo()
            a = await repo.create(input="running")
            b = await repo.create(input="waiting")
            c = await repo.create(input="cancelling")
            d = await repo.create(input="done")
            await repo.transition(a["id"], TaskState.RUNNING)
            await repo.transition(b["id"], TaskState.RUNNING)
            await repo.transition(b["id"], TaskState.WAITING_CONFIRM)
            await repo.transition(c["id"], TaskState.RUNNING)
            await repo.transition(c["id"], TaskState.CANCELLING)
            await repo.transition(d["id"], TaskState.SUCCEEDED)

            fixed = await recover_stale_tasks(repo)
            return fixed, [
                (await repo.get(x["id"]))["state"] for x in (a, b, c, d)
            ]

        fixed, states = run(scenario())
        self.assertEqual(fixed, 3)
        self.assertEqual(states, ["interrupted", "interrupted", "cancelled", "succeeded"])


class TestEchoDemo(unittest.TestCase):
    """echo 模式的演练规则：写动作 + 命中 L2 工具 → 真的停下来问。"""

    def test_echo_requests_confirmation_for_l2_tool(self):
        async def scenario():
            cfg = _config()
            repo = InMemoryTaskRepo()
            gate = PermissionGate(repo, timeout_s=0.2)
            from app.agent.toolbox import ToolCatalog, ToolRouter

            catalog = ToolCatalog.load(cfg.tools_path)
            runtime = EchoRuntime(
                delay=0.01,
                catalog=catalog,
                tool_router=ToolRouter(catalog),
                gate=gate,
                demo_confirm=True,
            )
            runner = TaskRunner(repo, TaskRegistry(), runtime, cfg)
            task = await runner.submit(user_input="今天记一笔 38 块打车")
            return await _wait_state(repo, task["id"], {"cancelled", "failed", "succeeded"}, 3.0)

        task = run(scenario())
        self.assertEqual(task["state"], "cancelled", "没人确认，超时后应取消")

    def test_readonly_input_does_not_stop(self):
        async def scenario():
            cfg = _config()
            repo = InMemoryTaskRepo()
            gate = PermissionGate(repo, timeout_s=0.2)
            from app.agent.toolbox import ToolCatalog, ToolRouter

            catalog = ToolCatalog.load(cfg.tools_path)
            runtime = EchoRuntime(
                delay=0.01,
                catalog=catalog,
                tool_router=ToolRouter(catalog),
                gate=gate,
                demo_confirm=True,
            )
            runner = TaskRunner(repo, TaskRegistry(), runtime, cfg)
            task = await runner.submit(user_input="上次和张总聊的定价结论是什么")
            return await _wait_state(repo, task["id"], {"cancelled", "failed", "succeeded"}, 3.0)

        task = run(scenario())
        self.assertEqual(task["state"], "succeeded", "只读提问不该触发确认")


if __name__ == "__main__":
    unittest.main()
