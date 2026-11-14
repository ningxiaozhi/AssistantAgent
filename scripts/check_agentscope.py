"""AgentScope 适配自检：把你机器上装的版本和我们代码里的假设对一遍。

    python scripts/check_agentscope.py

为什么要这个脚本：
  仓库里的 AgentScope 适配是照官方文档写的，但文档和实际安装版本之间总有差异
  （参数名、Toolkit 注册方式、Credential 构造）。与其让你跑起来报一个
  `TypeError: unexpected keyword 'tools'` 再猜，不如先跑这个，一次性列出所有不一致。

退出码：0 全部对得上；1 有需要改的地方（会明确告诉你改哪一行）。
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, BAD, SKIP = "✅", "❌", "⚠️ "


def _has(mod_name: str, attr: str) -> tuple[bool, object | None]:
    try:
        mod = importlib.import_module(mod_name)
    except ImportError:
        return False, None
    obj = getattr(mod, attr, None)
    return obj is not None, obj


def main() -> int:
    try:
        import agentscope
    except ImportError:
        print(f"{BAD} 未安装 agentscope。先执行：pip install 'agentscope>=2.0'")
        return 1

    print(f"agentscope 版本：{getattr(agentscope, '__version__', '未知')}\n")
    results: list[tuple[bool, str, str]] = []

    # ---- 1. Agent 构造参数 ----
    ok, Agent = _has("agentscope.agent", "Agent")
    if not ok:
        results.append((False, "agentscope.agent.Agent", "类不存在，包结构可能变了"))
        print_report(results)
        return 1

    params = list(inspect.signature(Agent.__init__).parameters)
    for name in ("name", "system_prompt", "model"):
        results.append((name in params, f"Agent(... {name}=)", f"现签名：{params}"))

    tools_key = next((k for k in ("tools", "toolkit") if k in params), None)
    results.append(
        (
            tools_key is not None,
            "Agent(... tools=)",
            f"实际参数名是 {tools_key or '（没有）'} → 改 app/agent/factory.py 的 CALIBRATE #1",
        )
    )

    # ---- 2. 对话入口 ----
    results.append((hasattr(Agent, "reply"), "Agent.reply()", "没有 reply，可能是 0.x 的 __call__ 接口"))
    results.append(
        (hasattr(Agent, "reply_stream"), "Agent.reply_stream()", "没有流式接口，SSE 会拿不到增量事件")
    )

    # ---- 3. ChatModel ----
    for cls in ("OllamaChatModel", "DeepSeekChatModel", "DashScopeChatModel", "OpenAIChatModel"):
        ok, obj = _has("agentscope.model", cls)
        results.append((ok, f"agentscope.model.{cls}", "类不存在 → 改 _build_chat_model"))

    ok, ollama = _has("agentscope.model", "OllamaChatModel")
    if ok and ollama is not None:
        mparams = list(inspect.signature(ollama.__init__).parameters)
        results.append(
            (
                "credential" in mparams,
                "ChatModel(credential=...)",
                f"现签名：{mparams} → 不是 credential 就改 _build_chat_model",
            )
        )

    # ---- 4. Credential ----
    for cls in ("OllamaCredential", "DeepSeekCredential", "DashScopeCredential", "OpenAICredential"):
        ok, _ = _has("agentscope.credential", cls)
        results.append((ok, f"agentscope.credential.{cls}", "不存在 → 该 provider 的凭据类改名了"))

    # ---- 5. Toolkit ----
    ok, Toolkit = _has("agentscope.toolkit", "Toolkit")
    if ok and Toolkit is not None:
        methods = [m for m in ("register", "add_function", "add", "register_tool") if hasattr(Toolkit, m)]
        results.append(
            (
                bool(methods),
                "Toolkit 注册工具",
                f"可用方法：{methods or '（都没找到）'} → 改 factory.py 的 CALIBRATE #2",
            )
        )
    else:
        results.append((False, "agentscope.toolkit.Toolkit", "找不到 Toolkit，工具挂不上"))

    # ---- 6. 有没有权限系统（如果有，我们的闸门可以和它合并）----
    ok, _ = _has("agentscope.permission", "PermissionMode")
    results.append((ok, "agentscope.permission.PermissionMode", "有的话可以接管 L0–L3，替换我们自研闸门"))

    print_report(results)
    failed = [r for r in results if not r[0]]
    print()
    if failed:
        print(f"{BAD} {len(failed)} 项对不上。按上面的提示改 app/agent/factory.py 里对应位置。")
        return 1
    print(f"{OK} 全部对得上，适配层应该可以直接用。")
    return 0


def print_report(results: list[tuple[bool, str, str]]) -> None:
    width = max(len(r[1]) for r in results) + 2
    for ok, name, hint in results:
        mark = OK if ok else BAD
        line = f"{mark} {name:<{width}}"
        print(line + ("" if ok else f"  ← {hint}"))


if __name__ == "__main__":
    raise SystemExit(main())
