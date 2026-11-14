"""Agent 运行时：一个接口，两个实现。

echo       —— 不需要任何模型就能跑通「状态机 → 安全点 → trace → 取消」整条链路
agentscope —— 真实 Agent（需自行校准 API 版本，见下方注释）

为什么保留 echo：骨架的第一要求是 `git clone && uvicorn` 就能跑。
没有模型的机器上也应该能点开 /docs 试一遍取消流程。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import Any, Protocol

from app.agent.routing import ModelRouter
from app.agent.tools import ToolRegistry, default_registry
from app.config import Settings
from app.core.cancel import TaskScope
from app.core.trace import Trace

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是用户的个人助理，负责学习、工作、生活三方面的事务。
规则：
1. 涉及对外发送、花钱、改日程、删文件的操作，先给预览再等确认
2. 回答前先看有没有相关记忆，不要凭空编造
3. 不确定就说不确定，不要猜
"""


class Runtime(Protocol):
    name: str

    async def run(
        self,
        *,
        task_id: str,
        user_input: str,
        scope: TaskScope,
        trace: Trace,
        sensitive: bool = False,
    ) -> str: ...


class EchoRuntime:
    """无模型回放：把 ReAct 的三步走一遍，每一步前后都是安全点。"""

    name = "echo"

    def __init__(self, steps: int = 3, delay: float = 0.12) -> None:
        self.steps = steps
        self.delay = delay

    async def run(
        self,
        *,
        task_id: str,
        user_input: str,
        scope: TaskScope,
        trace: Trace,
        sensitive: bool = False,
    ) -> str:
        plan = ["理解意图", "装配上下文", "生成回答"][: self.steps]
        for i, label in enumerate(plan, 1):
            trace.next_step()
            await scope.checkpoint("before_model_call")
            await trace.record("model.call", role="intent" if i == 1 else "subagent", step=label)
            await asyncio.sleep(self.delay)
            await scope.checkpoint("before_next_round")

        await trace.record("task.echo", note="echo 模式，未调用真实模型")
        return (
            f"（echo 模式）收到：「{user_input}」。\n"
            f"已走完 {len(plan)} 步并全程留痕；接上 AgentScope 后这里会是真实回答。"
        )


class AgentScopeRuntime:
    """真实 Agent。⚠️ EXPERIMENTAL：AgentScope 2.x 的构造参数请以你锁定的版本为准。

    对接顺序（与 eval/README.md 的六步一致）：
      1. router.resolve(role) → ModelSpec → 对应 provider 的 ChatModel
      2. Agent(name=..., system_prompt=..., model=...) 组装
      3. 每个安全点插 scope.checkpoint()
      4. 工具通过 ToolRegistry 暴露成 Toolkit，副作用走工具契约
      5. 回复里的 token/cost 写进 trace，供成本看板与门禁使用
    """

    name = "agentscope"

    def __init__(self, router: ModelRouter, settings: Settings, role: str = "planner") -> None:
        self.router = router
        self.settings = settings
        self.role = role
        self.tools: ToolRegistry = default_registry()

    async def run(
        self,
        *,
        task_id: str,
        user_input: str,
        scope: TaskScope,
        trace: Trace,
        sensitive: bool = False,
    ) -> str:
        try:
            from agentscope.agent import Agent
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("未安装 agentscope：pip install 'agentscope>=2.0'") from e

        resolved = self.router.resolve(self.role, sensitive=sensitive)
        await trace.record(
            "model.select",
            role=self.role,
            model=resolved.spec.ref,
            degraded=resolved.degraded,
            reason=resolved.reason,
        )

        model = _build_chat_model(resolved.spec, self.settings)
        agent = Agent(name="assistant", system_prompt=SYSTEM_PROMPT, model=model)

        await scope.checkpoint("before_model_call")
        reply = await agent.reply(user_input)
        await scope.checkpoint("before_next_round")
        await trace.record("model.call", role=self.role, model=resolved.spec.ref, ok=True)
        return _as_text(reply)


def _build_chat_model(spec: Any, settings: Settings) -> Any:
    """按 provider 造 ChatModel。provider 名称对应 models.yaml。"""
    from agentscope.model import (  # noqa: F401  (延迟导入，避免没装时影响启动)
        DashScopeChatModel,
        DeepSeekChatModel,
        OllamaChatModel,
        OpenAIChatModel,
    )
    from agentscope.credential import (  # noqa: F401
        DashScopeCredential,
        DeepSeekCredential,
        OllamaCredential,
        OpenAICredential,
    )

    p = spec.provider
    if p == "local":
        return OllamaChatModel(
            credential=OllamaCredential(base_url=spec.base_url or settings.ollama_base_url),
            model=spec.model,
        )
    if p == "ds":
        return DeepSeekChatModel(credential=DeepSeekCredential(api_key=spec.api_key), model=spec.model)
    if p == "qwen":
        return DashScopeChatModel(
            credential=DashScopeCredential(api_key=spec.api_key), model=spec.model
        )
    return OpenAIChatModel(credential=OpenAICredential(api_key=spec.api_key), model=spec.model)


def _as_text(reply: Any) -> str:
    """AgentScope 的返回可能是 Msg / str / 列表，统一成字符串。"""
    if isinstance(reply, str):
        return reply
    for attr in ("content", "text"):
        value = getattr(reply, attr, None)
        if isinstance(value, str):
            return value
    return str(reply)


def build_runtime(settings: Settings, router: ModelRouter) -> Runtime:
    backend = settings.agent_backend.lower()
    has_agentscope = importlib.util.find_spec("agentscope") is not None

    if backend == "echo":
        return EchoRuntime()
    if backend == "agentscope":
        if not has_agentscope:
            raise RuntimeError("AGENT_BACKEND=agentscope 但未安装：pip install 'agentscope>=2.0'")
        return AgentScopeRuntime(router, settings)
    # auto
    if has_agentscope:
        try:
            return AgentScopeRuntime(router, settings)
        except Exception as e:  # 配错模型也别让服务起不来
            log.warning("AgentScope 初始化失败(%s)，降级 echo", e)
            return EchoRuntime()
    log.info("未安装 agentscope，使用 echo 模式（状态机/取消/trace 全部可用）")
    return EchoRuntime()
