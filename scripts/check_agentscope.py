"""AgentScope 适配自检：把你机器上装的版本和我们代码里的假设对一遍。

    python scripts/check_agentscope.py
    python scripts/check_agentscope.py --json      # 机器可读

重要：这个脚本**只看已安装的 agentscope 包**，不看我们的代码。
所以 ❌ 不会因为你改了 factory.py 而消失——它的含义是
"你装的版本里没有这个东西"，你要做的是把 factory.py 改成它实际的样子。

退出码：0 全部对得上；1 有需要改的地方。
报告同时写入 agentscope-report.txt，方便直接贴给同事/贴进 issue。
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, BAD, WARN = "✅", "❌", "⚠️ "

# 符号可能出现的模块路径（不同小版本挪过位置）
AGENT_MODULES = ("agentscope.agent",)
MODEL_MODULES = ("agentscope.model",)
CRED_MODULES = ("agentscope.credential", "agentscope.credentials", "agentscope.model")
TOOLKIT_MODULES = ("agentscope.toolkit", "agentscope.tool", "agentscope.tools")
PERM_MODULES = ("agentscope.permission", "agentscope.permissions")

CHAT_MODELS = ("OllamaChatModel", "DeepSeekChatModel", "DashScopeChatModel", "OpenAIChatModel")
CREDENTIALS = ("OllamaCredential", "DeepSeekCredential", "DashScopeCredential", "OpenAICredential")


class Check:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, ok: bool, name: str, hint: str = "", fix: str = "") -> None:
        self.rows.append({"ok": ok, "name": name, "hint": hint, "fix": fix})

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if not r["ok"]]


def find(mod_names: tuple[str, ...], attr: str) -> tuple[bool, object | None, str | None]:
    for mod_name in mod_names:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        obj = getattr(mod, attr, None)
        if obj is not None:
            return True, obj, mod_name
    return False, None, None


def safe_signature(obj) -> list[str] | None:
    try:
        return list(inspect.signature(obj.__init__).parameters)
    except (ValueError, TypeError):
        return None


def main(argv: list[str] | None = None) -> int:
    as_json = "--json" in (argv or sys.argv[1:])
    try:
        import agentscope
    except ImportError:
        print(f"{BAD} 未安装 agentscope。先执行：pip install 'agentscope>=2.0'")
        return 1

    version = getattr(agentscope, "__version__", "未知")
    ck = Check()

    # ---- 1. Agent ----
    ok, Agent, where = find(AGENT_MODULES, "Agent")
    if not ok:
        ck.add(False, "agentscope.agent.Agent", "类不存在，包结构可能变了", "确认 pip show agentscope 的版本")
        return report(ck, version, as_json)

    params = safe_signature(Agent) or []
    for name in ("name", "system_prompt", "model"):
        ck.add(name in params, f"Agent(... {name}=)", f"实际签名：{params}",
               f"factory.py 里去掉 {name} 或改名" if name not in params else "")

    tools_key = next((k for k in ("tools", "toolkit", "tool_list") if k in params), None)
    ck.add(
        tools_key is not None,
        f"Agent(... {tools_key}=)",
        f"实际参数名是 {tools_key}" if tools_key else f"实际签名：{params}",
        f"把 factory.py 的 `tools=toolkit` 改成 `{tools_key}=toolkit`" if tools_key else
        "该版本 Agent 不接受工具参数，需要改用 Toolkit 的其它挂载方式",
    )

    has_reply = hasattr(Agent, "reply")
    ck.add(has_reply, "Agent.reply()", "没有 reply，可能是 0.x 的 __call__ 接口",
           "factory.py 里改成 await agent(msg)" if not has_reply else "")

    has_stream = hasattr(Agent, "reply_stream")
    ck.add(has_stream, "Agent.reply_stream()", "没有流式接口，SSE 拿不到增量事件",
           "流式降级：先只做一次性 reply" if not has_stream else "")

    # ---- 2. ChatModel ----
    for cls in CHAT_MODELS:
        ok, obj, where = find(MODEL_MODULES, cls)
        ck.add(ok, f"agentscope.model.{cls}", "类不存在", f"删掉 _build_chat_model 里 {cls} 的分支" if not ok else "")

    ok, ollama, _ = find(MODEL_MODULES, "OllamaChatModel")
    if ok and ollama is not None:
        mparams = safe_signature(ollama) or []
        uses_cred = "credential" in mparams
        ck.add(uses_cred, "ChatModel(credential=...)",
               f"现签名：{mparams}",
               "改 _build_chat_model：去掉 credential=，按签名传参" if not uses_cred else "")

    # ---- 3. Credential ----
    for cls in CREDENTIALS:
        ok, _, where = find(CRED_MODULES, cls)
        ck.add(ok, f"{cls}", f"在 {CRED_MODULES} 里都没找到",
               "如果 ChatModel 不收 credential，这个类可能也不存在，删掉对应分支" if not ok else "")

    # ---- 4. Toolkit ----
    ok, Toolkit, where = find(TOOLKIT_MODULES, "Toolkit")
    if ok and Toolkit is not None:
        methods = [m for m in ("register", "add_function", "add", "register_tool", "add_tool") if hasattr(Toolkit, m)]
        ck.add(bool(methods), "Toolkit 注册工具", f"可用方法：{methods or '（都没找到）'}",
               f"把 factory.py 的 toolkit.register 改成 toolkit.{methods[0]}" if methods else
               "该 Toolkit 没有注册接口，工具挂载方式需重写")
    else:
        ck.add(False, "Toolkit", f"在 {TOOLKIT_MODULES} 里都没找到", "找不到就不挂工具（factory.py 会自动降级）")

    # ---- 5. 权限系统 ----
    ok, _, where = find(PERM_MODULES, "PermissionMode")
    ck.add(ok, "PermissionMode", "有的话可以用它接管 L0–L3",
           "" if ok else "没有就用我们的 PermissionGate（默认就是这样）")

    return report(ck, version, as_json)


def report(ck: Check, version: str, as_json: bool) -> int:
    failed = ck.failed
    if as_json:
        print(json.dumps({"version": version, "rows": ck.rows, "failed": len(failed)}, ensure_ascii=False, indent=2))
        return 0 if not failed else 1

    print(f"agentscope 版本：{version}\n")
    width = max(len(r["name"]) for r in ck.rows) + 2
    for r in ck.rows:
        mark = OK if r["ok"] else BAD
        line = f"{mark} {r['name']:<{width}}"
        if not r["ok"]:
            line += f"  {r['hint']}"
        print(line)

    lines = [f"agentscope {version}", ""]
    lines += [f"{OK if r['ok'] else BAD} {r['name']}" + ("" if r["ok"] else f" — {r['hint']}") for r in ck.rows]

    if not failed:
        print(f"\n{OK} 全部对得上，适配层应该可以直接用。")
        print(f"   下一步：$env:AGENT_BACKEND=\"agentscope\" 然后启动服务，/health 里 agent 应为 agentscope")
        _save(lines, 0)
        return 0

    print(f"\n{BAD} {len(failed)} 项对不上。**注意：这里描述的是你装的包，不是我们的代码。**")
    print("   改完之后这些 ❌ 也不会自动消失 —— 改对了它们才消失。\n")
    print("要改的地方：")
    for r in failed:
        if r["fix"]:
            print(f"  · {r['name']}")
            print(f"      → {r['fix']}")
    print("\n把下面这段贴给我就能定位：")
    print("  " + "─" * 56)
    for r in failed:
        print(f"  {r['name']} | {r['hint']}")
    print("  " + "─" * 56)

    _save(lines, 1)
    return 1


def _save(lines: list[str], code: int) -> None:
    try:
        Path(ROOT / "agentscope-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n（报告已写入 agentscope-report.txt）")
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
