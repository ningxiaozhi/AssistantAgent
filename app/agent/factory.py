"""Agent 运行时：一个接口，两个实现。

echo       —— 不需要任何模型就能跑通「状态机 → 安全点 → trace → 取消」整条链路
agentscope —— 真实 Agent（需自行校准 API 版本，见下方注释）

两个运行时共用同一套工具治理：**先按 profile 定可见范围，再按输入定库**，
只有选中的库里的工具才会进 prompt。echo 模式也会把这次选择记进 trace，
所以"工具路由"这件事在没有模型的情况下也能验证。

系统提示、工具表都来自 config/，改行为不用改代码。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import Any, Protocol

from app.agent.routing import ModelRouter
from app.agent.toolbox import ToolCatalog, ToolRouter, ToolSelection
from app.config import AppConfig
from app.core.cancel import TaskScope
from app.core.trace import Trace

log = logging.getLogger(__name__)

FALLBACK_SYSTEM_PROMPT = """你是用户的个人助理，负责学习、工作、生活三方面的事务。
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
    """无模型回放：选库 → 走三步 ReAct，每一步前后都是安全点。"""

    name = "echo"

    def __init__(
        self,
        steps: int = 3,
        delay: float = 0.12,
        catalog: ToolCatalog | None = None,
        tool_router: ToolRouter | None = None,
    ) -> None:
        self.steps = steps
        self.delay = delay
        self.catalog = catalog
        self.tool_router = tool_router

    async def run(
        self,
        *,
        task_id: str,
        user_input: str,
        scope: TaskScope,
        trace: Trace,
        sensitive: bool = False,
    ) -> str:
        selection = self._select(user_input, trace)
        await self._record_selection(selection, trace)

        plan = ["理解意图", "装配上下文", "生成回答"][: self.steps]
        for i, label in enumerate(plan, 1):
            trace.next_step()
            await scope.checkpoint("before_model_call")
            await trace.record("model.call", role="intent" if i == 1 else "subagent", step=label)
            await asyncio.sleep(self.delay)
            await scope.checkpoint("before_next_round")

        await trace.record("task.echo", note="echo 模式，未调用真实模型")
        libs = "、".join(selection.libraries) if selection else "（无）"
        return (
            f"（echo 模式）收到：「{user_input}」。\n"
            f"已选定工具库：{libs}；走完 {len(plan)} 步并全程留痕。\n"
            f"接上 AgentScope 后这里会是真实回答。"
        )

    def _select(self, text: str, trace: Trace) -> ToolSelection | None:
        if self.tool_router is None:
            return None
        try:
            return self.tool_router.route(text)
        except Exception as e:  # 路由失败不该拖垮任务
            log.warning("工具路由失败: %s", e)
            return None

    async def _record_selection(self, selection: ToolSelection | None, trace: Trace) -> None:
        if selection is None:
            return
        await trace.record(
            "tools.select",
            libraries=selection.libraries,
            tools=selection.tools,
            reason=selection.reason,
        )


class AgentScopeRuntime:
    """真实 Agent。⚠️ EXPERIMENTAL：AgentScope 2.x 的构造参数请以你锁定的版本为准。

    对接顺序（与 eval/README.md 的六步一致）：
      1. 先选库：tool_router.route() → 只把选中的库展开成工具表
      2. router.resolve(role) → ModelSpec → 对应 provider 的 ChatModel
      3. Agent(name=..., system_prompt=..., model=...) 组装
      4. 每个安全点插 scope.checkpoint()
      5. 回复里的 token/cost 写进 trace，供成本看板与门禁使用
    """

    name = "agentscope"

    def __init__(
        self,
        router: ModelRouter,
        config: AppConfig,
        catalog: ToolCatalog | None = None,
        role: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.router = router
        self.config = config
        self.catalog = catalog
        self.tool_router = ToolRouter(catalog) if catalog else None
        self.role = role or config.runtime.default_role
        self.profile = profile
        self.system_prompt = config.read_prompt("system.md", FALLBACK_SYSTEM_PROMPT)

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

        # --- 分层取工具：先定库，再取工具，只有这些 schema 进 prompt ---
        selection = None
        tools = None
        if self.tool_router is not None and self.catalog is not None:
            selection = self.tool_router.route(user_input, profile=self.profile)
            tools = self.catalog.resolve(selection)
            await trace.record(
                "tools.select",
                profile=self.profile,
                libraries=selection.libraries,
                tools=selection.tools,
                reason=selection.reason,
            )

        resolved = self.router.resolve(self.role, sensitive=sensitive)
        await trace.record(
            "model.select",
            role=self.role,
            model=resolved.spec.ref,
            degraded=resolved.degraded,
            reason=resolved.reason,
        )

        model = _build_chat_model(resolved.spec)
        agent = Agent(name="assistant", system_prompt=self.system_prompt, model=model)

        await scope.checkpoint("before_model_call")
        reply = await agent.reply(user_input)
        await scope.checkpoint("before_next_round")
        await trace.record("model.call", role=self.role, model=resolved.spec.ref, ok=True)
        return _as_text(reply)


def _build_chat_model(spec: Any) -> Any:
    """按 provider 造 ChatModel。provider 名称对应 config/models.yaml。"""
    from agentscope.credential import (  # noqa: F401  (延迟导入，没装时不影响启动)
        DashScopeCredential,
        DeepSeekCredential,
        OllamaCredential,
        OpenAICredential,
    )
    from agentscope.model import (  # noqa: F401
        DashScopeChatModel,
        DeepSeekChatModel,
        OllamaChatModel,
        OpenAIChatModel,
    )

    p = spec.provider
    if p == "local":
        return OllamaChatModel(
            credential=OllamaCredential(base_url=spec.base_url), model=spec.model
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


def build_runtime(
    config: AppConfig, router: ModelRouter, catalog: ToolCatalog | None = None
) -> Runtime:
    backend = config.runtime.agent_backend.lower()
    has_agentscope = importlib.util.find_spec("agentscope") is not None
    tool_router = ToolRouter(catalog) if catalog else None

    if backend == "echo":
        return EchoRuntime(catalog=catalog, tool_router=tool_router)
    if backend == "agentscope":
        if not has_agentscope:
            raise RuntimeError("agent_backend=agentscope 但未安装：pip install 'agentscope>=2.0'")
        return AgentScopeRuntime(router, config, catalog)
    # auto
    if has_agentscope:
        try:
            return AgentScopeRuntime(router, config, catalog)
        except Exception as e:  # 配错模型也别让服务起不来
            log.warning("AgentScope 初始化失败(%s)，降级 echo", e)
            return EchoRuntime(catalog=catalog, tool_router=tool_router)
    log.info("未安装 agentscope，使用 echo 模式（状态机/取消/trace 全部可用）")
    return EchoRuntime(catalog=catalog, tool_router=tool_router)
