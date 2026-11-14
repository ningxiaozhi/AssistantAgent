"""AgentScope 适配层的约定测试：装了就跑，没装就跳过。

价值在于：你 `pip install agentscope` 之后，跑一次 `python -m unittest` 就能知道
仓库里的适配代码和实际版本对不对得上——而不是等到线上报 TypeError。

更细的诊断（打印完整签名）用：`python scripts/check_agentscope.py`
"""
import importlib.util
import inspect
import unittest

HAS_AGENTSCOPE = importlib.util.find_spec("agentscope") is not None


@unittest.skipUnless(HAS_AGENTSCOPE, "未安装 agentscope，跳过适配层约定测试")
class TestAgentScopeContract(unittest.TestCase):
    def test_agent_accepts_our_kwargs(self):
        from agentscope.agent import Agent

        params = inspect.signature(Agent.__init__).parameters
        for name in ("name", "system_prompt", "model"):
            self.assertIn(name, params, f"Agent 不再接受 {name}，需改 factory.py")
        self.assertTrue(
            {"tools", "toolkit"} & set(params),
            f"Agent 既没有 tools 也没有 toolkit 参数，实际为 {list(params)}",
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

    def test_reply_exists(self):
        from agentscope.agent import Agent

        self.assertTrue(hasattr(Agent, "reply"), "Agent 没有 reply()")

    def test_runtime_imports(self):
        """适配层本身能导入（即使不真跑）。"""
        from app.agent.factory import AgentScopeRuntime

        self.assertEqual(AgentScopeRuntime.name, "agentscope")


if __name__ == "__main__":
    unittest.main()
