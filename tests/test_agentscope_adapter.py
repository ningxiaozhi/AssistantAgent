"""AgentScope 适配层的约定测试：装了就跑，没装就跳过。

价值在于：你 `pip install agentscope` 之后，跑一次 `python -m unittest` 就能知道
仓库里的适配代码和实际版本对不对得上——而不是等到线上报 TypeError。

当前已对 agentscope 2.0.11 校准：
  · Agent(..., toolkit=)          ← 参数名是 toolkit
  · agentscope.tool.Toolkit       ← 不是 agentscope.toolkit
  · toolkit.add_tool(...)         ← 是协程，要 await
  · FunctionTool 的函数返回 ToolChunk(content=[TextBlock(...)])
"""
import asyncio
import importlib.util
import inspect
import unittest

HAS_AGENTSCOPE = importlib.util.find_spec("agentscope") is not None
HAS_OLLAMA = importlib.util.find_spec("ollama") is not None


async def _maybe(value):
    return await value if inspect.isawaitable(value) else value


@unittest.skipUnless(HAS_AGENTSCOPE, "未安装 agentscope，跳过适配层约定测试")
class TestAgentScopeContract(unittest.TestCase):
    def test_agent_accepts_our_kwargs(self):
        from agentscope.agent import Agent

        params = inspect.signature(Agent.__init__).parameters
        for name in ("name", "system_prompt", "model"):
            self.assertIn(name, params, f"Agent 不再接受 {name}，需改 factory.py")
        self.assertIn(
            "toolkit", params, f"Agent 没有 toolkit 参数，实际为 {list(params)}"
        )

    def test_chat_models_exist(self):
        from agentscope.model import (  # noqa: F401
            DashScopeChatModel,
            DeepSeekChatModel,
            OllamaChatModel,
            OpenAIChatModel,
        )

    def test_chat_model_uses_credential(self):
        from agentscope.model import OllamaChatModel

        params = inspect.signature(OllamaChatModel.__init__).parameters
        self.assertIn("credential", params, "ChatModel 构造参数不再是 credential")

    def test_reply_接口存在(self):
        from agentscope.agent import Agent

        self.assertTrue(hasattr(Agent, "reply"), "Agent 没有 reply()")
        self.assertTrue(inspect.iscoroutinefunction(Agent.reply), "reply 不再是协程")

    def test_toolkit_在主路径(self):
        from agentscope.tool import Toolkit

        self.assertTrue(hasattr(Toolkit, "add_tool"), "Toolkit 没有 add_tool")


@unittest.skipUnless(HAS_AGENTSCOPE, "未安装 agentscope，跳过工具桥接测试")
class TestToolkitBridge(unittest.TestCase):
    """真正把工具装配进 Toolkit 并调一次 —— 不经过模型，也不需要网络。"""

    def test_registers_and_calls(self):
        async def scenario():
            from app.agent.factory import _build_toolkit
            from app.agent.toolbox import ToolCatalog, ToolRouter
            from app.config import AppConfig

            cfg = AppConfig.load()
            catalog = ToolCatalog.load(cfg.tools_path)
            registry = catalog.resolve(ToolRouter(catalog).route("明天有什么会议"))
            toolkit = await _build_toolkit(registry)
            self.assertIsNotNone(toolkit, "Toolkit 装配失败")

            schemas = await _maybe(toolkit.get_tool_schemas())
            self.assertEqual(len(schemas), len(registry), "装配的工具数与注册表不一致")

            read_only = next(t for t in registry if t.side_effect == "none")
            tool = await _maybe(toolkit.get_tool(read_only.name.replace(".", "_")))
            self.assertIsNotNone(tool, f"Toolkit 里找不到 {read_only.name}")

            chunk = await _maybe(tool.call())
            self.assertTrue(getattr(chunk, "content", None), "工具调用没有返回内容")
            self.assertIn("ok", str(chunk.content[0].text))

        asyncio.run(scenario())


@unittest.skipUnless(HAS_AGENTSCOPE and HAS_OLLAMA, "缺少 agentscope 或 ollama 包")
class TestAgentConstruction(unittest.TestCase):
    def test_constructs_with_model_and_toolkit(self):
        async def scenario():
            from agentscope.agent import Agent

            from app.agent.factory import _build_chat_model, _build_toolkit
            from app.agent.routing import ModelRouter
            from app.agent.toolbox import ToolCatalog, ToolRouter
            from app.config import AppConfig

            cfg = AppConfig.load()
            router = ModelRouter(cfg.models_path, cfg.runtime.force_local_on_sensitive)
            model = _build_chat_model(router.resolve("intent").spec)

            catalog = ToolCatalog.load(cfg.tools_path)
            registry = catalog.resolve(ToolRouter(catalog).route("记一下"))
            toolkit = await _build_toolkit(registry)

            agent = Agent(
                name="assistant",
                system_prompt=cfg.read_prompt("system.md"),
                model=model,
                toolkit=toolkit,
            )
            self.assertIsNotNone(agent)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
