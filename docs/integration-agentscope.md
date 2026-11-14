# 接真实 AgentScope：四步 + 三个校准点

> 现状：仓库里的 `AgentScopeRuntime` 是照官方文档写的，**没有对着真实安装版本跑过**
> （开发机上 PyPI 不可达）。下面这套流程的每一步都有对应的验证手段，
> 照着走，20 分钟内能确认它到底能不能用。

---

## 第 1 步：装

```bash
pip install "agentscope>=2.0"
python -c "import agentscope; print(agentscope.__version__)"
```

> 如果公司网络有镜像源：`pip install -i https://mirrors.aliyun.com/pypi/simple "agentscope>=2.0"`

---

## 第 2 步：自检（**先做这一步，别直接启动**）

```bash
python scripts/check_agentscope.py
```

它会把你装的版本和我们代码里的假设逐条对一遍，输出类似：

```
agentscope 版本：2.0.x

✅ Agent(... system_prompt=)
✅ Agent(... tools=)
❌ Agent.reply_stream()                       ← 没有流式接口，SSE 会拿不到增量事件
✅ agentscope.model.OllamaChatModel
✅ ChatModel(credential=...)
✅ Toolkit 注册工具
⚠️  agentscope.permission.PermissionMode      ← 有的话可以接管 L0–L3
```

**全部 ✅ 就往下走；有 ❌ 就按提示改 `app/agent/factory.py` 里标了 `CALIBRATE` 的位置。**

---

## 第 3 步：配模型

本地 Ollama（最省事，也不出网）：

```bash
ollama pull qwen2.5:7b
ollama serve            # 默认 127.0.0.1:11434
```

`config/models.yaml` 里已经指向它：

```yaml
providers:
  local:
    type: ollama
    base_url: http://127.0.0.1:11434
roles:
  planner:
    model: local/qwen2.5-7b      # ← 想快速验证，把 planner 也改成本地
```

外部模型只要填 key：

```bash
export DEEPSEEK_API_KEY=sk-xxx      # Windows: $env:DEEPSEEK_API_KEY="sk-xxx"
```

---

## 第 4 步：切后端并跑通

```bash
# 强制用 agentscope（配错会直接报错，不会静默退化到 echo）
$env:AGENT_BACKEND="agentscope"     # bash: export AGENT_BACKEND=agentscope

uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000/docs>，先 `GET /health`：

```json
{"status":"ok","agent":"agentscope", ...}
```

`agent` 是 `agentscope` 就说明装配成功；是 `echo` 说明降级了，去看启动日志里的
`AgentScope 初始化失败(...)`。

然后发一条最简单的：

```bash
curl -X POST localhost:8000/api/chat -H "Content-Type: application/json" \
     -d '{"message":"今天几号？"}'
```

---

## 三个校准点（对不上就改这三处）

| 位置 | 我们假设的写法 | 对不上时怎么改 |
|---|---|---|
| **CALIBRATE #1** | `Agent(..., tools=toolkit)` | `check_agentscope.py` 会说实际参数名是 `toolkit` 还是 `tools` |
| **CALIBRATE #2** | `toolkit.register(fn)` | 有的版本是 `add_function` / `add` / 装饰器 |
| **CALIBRATE #3** | `ChatModel(credential=..., model=...)` | 自检会打印真实签名；不是 `credential` 就按签名改 |

改完之后 `python -m unittest tests.test_agentscope_adapter` 应该全绿——
这组测试就是专门用来钉这三个校准点的。

---

## 接上之后立刻会遇到的四件事

### 1. 工具真的会被调用吗

现在 `_build_toolkit()` 会把**已选库**里的工具注册给 Agent（工具治理生效），
但模型会不会调用、调用参数对不对，需要看 trace：

```bash
curl localhost:8000/api/tasks/<id>/trace
```

如果 `tools.select` 里没有你期望的库 → 是路由问题（改 `config/tools.yaml` 的 triggers）。
如果库选对了但模型没用工具 → 是工具描述问题（改 `description`，这是唯一该动的地方）。

### 2. 确认闸门要不要留

AgentScope 自己有 `PermissionMode`（DEFAULT / EXPLORE / DONT_ASK…）。
两套不要并存，二选一：

- **想用它的**：`PermissionMode` 接管 L0–L3，我们的 `PermissionGate` 关掉
- **想用我们的**：把 Agent 配成 `DONT_ASK`，由我们的闸门决定何时停（推荐——因为我们的闸门和任务状态机、trace、补偿是连着的）

### 3. 超时与步数

`limits.max_steps` / `max_usd_per_task` 目前只在配置里，**没有真正强制**。
接上真模型后第一件事就是把它接进循环，否则一次任务能烧掉几块钱。

### 4. 流式

`reply_stream` 能拿到增量事件，接上之后 SSE 才有意义。
如果自检显示没有 `reply_stream`，退化成一次性 `reply` 也能用，只是前端要等。

---

## 排查顺序（卡住时按这个来）

1. `python scripts/check_agentscope.py` —— 版本对不对
2. `GET /health` 里 `agent` 是不是 `agentscope`
3. 启动日志有没有 `AgentScope 初始化失败` / `找不到 Toolkit`
4. 单独跑一个最小脚本（不经过 FastAPI）：

```python
import asyncio
from agentscope.agent import Agent
from agentscope.model import OllamaChatModel
from agentscope.credential import OllamaCredential

async def main():
    agent = Agent(
        name="t",
        system_prompt="你是助理",
        model=OllamaChatModel(
            credential=OllamaCredential(base_url="http://127.0.0.1:11434"),
            model="qwen2.5:7b",
        ),
    )
    print(await agent.reply("你好"))

asyncio.run(main())
```

这个脚本能跑通，说明问题在适配层；跑不通，说明问题在模型/凭据。
