# 个人助理 Agent · 架构说明（v2）

> 配套图：`assistant-architecture.png`　可编辑源：`assistant-architecture.html`
> 技术基线：**Python 3.11+ / FastAPI / AgentScope 2.0 / PostgreSQL 16 + pgvector 0.8**

---

## 0. 一句话架构

**一个入口，六层分工，五条铁律。**

入口收在微信/语音；六层把"感知 → 思考 → 动手 → 记住 → 有边界"拆开；五条铁律（权限、隐私、成本、评估、可观测）横穿所有层。

三个总原则，决定了下面所有细节：

| 原则 | 含义 | 反面 |
|---|---|---|
| **框架托底，不自己造轮子** | AgentScope 2.0 已提供 Agent 循环、权限、HITL、中断、记忆、RAG、Service 化，我们只写"业务与运维"那 20% | 自己手写 ReAct 循环 + 消息总线，三个月后卡在并发与状态恢复 |
| **一个库，一条真相** | Postgres 扛住关系数据 + 向量 + 日志 + 状态快照 | 先 SQLite、后迁 PG，向量维度全量重算，记忆一次信任危机 |
| **可中断、可复盘** | 任何任务随时能停；任何异常都能还原"当时它看到了什么" | 出问题只能看 `print` 日志，重跑一次复现不了 |

---

## 1. 技术基线：每一层落在谁身上

| 架构层 | AgentScope 2.0 提供 | 我们要写的 |
|---|---|---|
| 触点层 | — | 微信/Telegram 适配器、ASR/TTS 接入 |
| 接入网关 | Agent Service（`create_app`）已含会话、SSE、后台任务、定时器、中断 | 渠道适配 + 富媒体解析 + 任务表 |
| 智能体核心 | `Agent`（ReAct 循环）、`Plan`、`Permission`、`Middleware`、`Agent Team` | 子代理编排、路由策略、模型绑定 |
| 能力层 | `Tool` / MCP、Tool Middleware | 业务工具（日历、邮件、笔记…）+ 幂等与补偿 |
| 记忆与知识 | `AgenticMemoryMiddleware`（Markdown wiki）、`KnowledgeBase`（RAG） | 多库划分、**pgvector 的 VectorStore 实现**、wiki 维护任务 |
| 模型与底座 | `Credential` + `ChatModelBase`（OpenAI / Anthropic / DashScope / DeepSeek / Ollama …）、内置 Tracing 与 Budget Control | 角色化路由配置、pgvector 存储、部署与观测 |

**一句话**：AgentScope 2.0 的边界刚好切在"引擎"和"业务"之间，Pgvector 是唯一需要自己补的构件（官方内置的是 Qdrant / Milvus Lite）。

参考：[Agent](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/agent)、[Permission System](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/permission-system)、[Middleware](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/middleware)、[Long-Term Memory](https://docs.agentscope.io/versions/2.0.4/en/building-blocks/long-term-memory)、[Agent Service](https://docs.agentscope.io/versions/2.0.4/en/deploy/agent-service)

---

## 2. 模型：角色化路由，本地/外部自由组合

### 2.1 配置形态

一个 YAML 描述"谁用哪个模型"，启动时装配成 `ChatModel` 实例注入 Agent：

```yaml
# config/models.yaml
providers:
  local:   {type: ollama,     base_url: "http://127.0.0.1:11434"}
  ds:      {type: deepseek,   api_key_ref: "env:DEEPSEEK_API_KEY"}
  qwen:    {type: dashscope,  api_key_ref: "env:DASHSCOPE_API_KEY"}

roles:
  intent:            {model: local/qwen2.5-7b-instruct, temperature: 0.0, max_tokens: 200}
  rewrite:           {model: local/qwen2.5-7b-instruct, temperature: 0.3}
  planner:           {model: ds/deepseek-chat,          temperature: 0.2}
  subagent.learning: {model: ds/deepseek-chat,          temperature: 0.4}
  subagent.work:     {model: ds/deepseek-chat,          temperature: 0.3, fallback: [qwen/qwen-max]}
  subagent.life:     {model: local/qwen2.5-32b-instruct, temperature: 0.5}
  subagent.research: {model: qwen/qwen-max,             temperature: 0.3}
  summarize:         {model: local/qwen2.5-7b-instruct, temperature: 0.2}
  embed:             {model: local/bge-m3, dimension: 1024}   # 全局唯一，换它=全量重算
```

`api_key_ref` 只存引用不存明文，实际值走环境变量 / 系统密钥库。

### 2.2 路由矩阵：为什么这么分

| 角色 | 典型选择 | 约束 | 理由 |
|---|---|---|---|
| **意图理解** `intent` | 本地 7B | P95 < 300ms，输出结构化 JSON | 高频、简单、**每条消息都过**，用贵模型纯属浪费；本地还省一次出网 |
| **问题改写** `rewrite` | 本地 7B | P95 < 500ms | 把"那个事"补全成可检索的 query，不需要强推理 |
| **规划器** `planner` | 外部强模型 | 步数 ≤ 8 | 唯一真正需要"想得深"的环节，这里省钱会连环出错 |
| **子代理** | 分领域 | 各自独立 | 工作用强模型（写作/邮件质量敏感），生活用本地（省钱且涉隐私） |
| **摘要/压缩** `summarize` | 本地小模型 | 输出 ≤ 300 字 | 量大、容错高，失败可重试 |
| **Embedding** | 本地 bge-m3 | **维度 1024 全局锁定** | 换 embedding = 全库重算，这是最不该变的一环 |

### 2.3 三条硬约束

1. **敏感数据强制本地**：任务打上 `sensitivity: high`（含身份证、病历、账户、私聊），路由层直接改写为 `local/*`，即使外面更便宜。这条在网关层判定，不信模型自己报。
2. **运行中的任务不热切换模型**。配置变更对**新任务**生效；老任务继续用启动时的 `model_version`，并把这个版本写进 trace。否则一次配置热更，会让长任务后半段"换了个人"。
3. **降级要有链**：`主模型 → fallback → 本地兜底`，连续失败 3 次熔断 5 分钟（用 AgentScope 的 model fallback 中间件模式实现）。降级事件必须打标 `degraded=true`，否则你会在不知情的情况下用了三个月的降级模型。

### 2.4 成本守卫

AgentScope 有内置 **Budget Control** 中间件。我们的配置：

- 单任务上限：`max_steps=8`、`max_tokens=120k`、`max_usd=0.15`
- 单日上限：软阈值提醒，硬阈值拒绝新任务并告知
- 超限行为：不是直接失败，而是**降级为"给你半成品 + 说明还差什么"**

---

## 3. 存储：Postgres + pgvector，一步到位

### 3.1 为什么不要"先 SQLite 后迁移"

| 迁移代价 | 具体后果 |
|---|---|
| 向量索引重建 | 维度/距离函数/索引参数一变，全部 chunk 重新 embedding，钱和时间都是其次 |
| 语义漂移 | 迁移期间老数据用老模型、新数据用新模型，检索结果不可比 |
| 时间语义 | SQLite 没有真正的 `timestamptz`，记忆的"什么时候说的"会失真 |
| 信任成本 | 记忆是长期资产，迁移一次出一次错，你就再也不敢让它"记住"了 |

个人助理的数据量（十万级 chunk、百万级日志）**单机 Postgres 完全够**，没有理由分阶段。

### 3.2 表清单（schema `app`）

| 表 | 作用 | 关键字段 |
|---|---|---|
| `session` | 会话 | `id, channel, user_id, created_at` |
| `agent_state` | AgentScope 状态快照（支持暂停/恢复） | `session_id, state jsonb, version` |
| `task` | 任务状态机（见 §5） | `id, state, version, cancel_requested_at, parent_id, model_version` |
| `task_step` | 步骤时间线 | `task_id, step_no, kind, started_at, ended_at` |
| `llm_call` | 每次模型调用 | 见 §6.3 |
| `tool_call` | 每次工具调用 | `tool, args_digest, status, idempotency_key, side_effect` |
| `permission_event` | 权限决策 | `task_id, tool, decision, rule, decided_by` |
| `memory_fact` | 结构化事实 | `subject, predicate, object, confidence, valid_from, valid_to` |
| `wiki_page` | LLM wiki 页面元数据 | `kb_key, path, type, summary, updated_at` |
| `kb_chunk` | wiki 正文块 + 向量 | `kb_key, page_path, content, embedding` |

**VectorStore 自实现**：继承 AgentScope 的 `VectorStoreBase`，用 `psycopg` 直连 pgvector，注册给 `KnowledgeBase` handle。这样 `insert_document / search / delete_document` 四个接口不变，上层无感。

```sql
CREATE EXTENSION IF NOT EXISTS vector;      -- schema vec

CREATE TABLE vec.kb_chunk (
  id          bigserial PRIMARY KEY,
  kb_key      text NOT NULL,                -- me / work / learn / life / ref
  page_path   text NOT NULL,                -- wiki 页面，如 learn/ddia-ch4.md
  seq         int  NOT NULL DEFAULT 0,
  content     text NOT NULL,
  metadata    jsonb NOT NULL DEFAULT '{}',
  embedding   halfvec(1024) NOT NULL,       -- pgvector >= 0.7
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ON vec.kb_chunk USING hnsw (embedding halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 200);
CREATE INDEX ON vec.kb_chunk (kb_key, page_path);
```

`kb_key` 隔离多库（不用多表，便于跨库检索时一次查询）；`page_path` 让"检索命中"能直接回到 wiki 页面原文。

### 3.3 备份与恢复

`pg_dump` 一份 = 记忆 + 日志 + 状态 + 向量全都有。每周自动 dump 到对象存储，保留 8 周。

---

## 4. 记忆：LLM Wiki + 按类型分库

### 4.1 什么是"LLM wiki"

不是"把文档切块丢进向量库"，而是：**知识以人可读的 Markdown 页面存在，由 LLM 自己负责创建、合并、维护。**

一页长这样：

```markdown
---
name: DDIA 第4章 编码与演化
description: 数据编码格式的取舍，用于回答"为什么不用 JSON 存事件"
type: learn
tags: [分布式, 序列化]
updated: 2026-11-12
sources: [inbox/2026-11-10-ddia-ch4.pdf]
---

## 结论
Schema 演化有两条路：向后兼容（新读旧）与向前兼容（旧读新）。
Avro 靠 writer/reader schema 解决，Protobuf 靠字段编号 + 可选字段。

## 我的用法
事件表用 Protobuf；日志用 JSON（可读性优先）。

## 相关
[[DDIA 第3章 存储引擎]] · [[事件驱动架构取舍]]
```

三个关键点：

1. **有 frontmatter**：`name / description / type / tags`。`description` 是给检索用的，不是给人看的——写不好就检索不到。
2. **每个库一个 `INDEX.md`**：只放"页名 + 一句话"，注入系统提示。LLM 先看索引，再决定读哪页（**渐进式披露**），而不是一上来就把全文塞进上下文。
3. **写入是"改写"，不是"追加"**：新材料进来先找已有页面，能合并就合并、能替换就替换，只有真新的概念才建页。否则半年后同一个概念有 8 个版本。

> AgentScope 2.0 的 `AgenticMemoryMiddleware` 就是这套东西的原生实现：Markdown 文件 + 固定的 `MEMORY.md` 索引 + frontmatter（`user / feedback / project / reference` 四类）+ 自动注入索引 + 异步检索注入 `HintBlock`。我们**复用它做单库引擎，用"多实例 + 多目录"实现多库**。

### 4.2 五个库，按知识类型分

| 库 `kb_key` | 装什么 | 更新频率 | 检索方式 | 谁来写 |
|---|---|---|---|---|
| `me` | 画像、偏好、作息、关系、雷区 | 高 | **常驻注入**（不检索） | 主循环 |
| `work` | 项目、会议、决策、人名、承诺 | 高 | 精确（SQL）+ 语义 | 工作助理 |
| `learn` | 读书笔记、课程、概念卡、错题 | 中 | 语义 + 双链扩展 | 学习教练 |
| `life` | 健康、财务、家事、旅行 | 低 | 精确（SQL）+ 语义 | 生活管家 |
| `ref` | 剪藏、PDF 归档、外部资料 | 低 | 纯 RAG（只读） | 研究助手 |

**为什么要分库**：不同库的"检索胃口"完全不同。`me` 要全量常驻（就几句话），`ref` 要 Top-K 召回（几万篇）。混在一个库，要么全塞、要么全召，怎么调都不对。

**子代理与库的绑定矩阵**（写死在配置里，不靠模型自觉）：

| 子代理 | 可读 | 可写 |
|---|---|---|
| 学习教练 | `learn` `me` | `learn` |
| 工作助理 | `work` `me` `ref` | `work` |
| 生活管家 | `life` `me` | `life` |
| 研究助手 | `ref` `learn` | `ref`（只追加） |

好处：工作助理永远污染不了你的学习笔记；研究助手抓来的网页也不会混进"我的结论"。

### 4.3 写入流水线

```
对话/文档 → 摘要 → 抽候选事实(人/事/偏好/结论)
         → 判库(kb_key) → 找已有页面(语义 >0.85 视为同页)
         → 改写/合并/新建 → 更新 INDEX.md → 写 kb_chunk(带向量)
```

**必须人工过一眼**：低置信度（<0.6）或涉及"承诺、截止日、金额"的写入，先落 `inbox`，第二天早报里一句"昨天记了这三条，对吗？"。这是防记忆污染最便宜的手段。

### 4.4 维护任务（定时）

| 频率 | 做什么 |
|---|---|
| 每日 | 生成 `inbox` 候选；过期页面打 `stale` 标记 |
| 每周 | 同库内去重、合并、拆分过长页（>1500 字）；孤儿页（无入链）检查 |
| 每月 | 索引健康检查：空描述、断链、embedding 与页面不同步 |
| 换模型时 | 全库重算 embedding（**唯一必须停机的事**） |

### 4.5 反例：wiki 腐化的三种死法

- **流水账化**：把每天对话原文存进 wiki → 半年后 4000 页，索引本身就把上下文撑爆
- **只增不改**：同一件事有 5 个版本，检索到最老的那个，回答得还很自信
- **描述敷衍**：frontmatter 的 `description` 写成"关于 XX"，等于没写，检索永不命中

---

## 5. 任务生命周期与取消

这是个人助理和"聊天机器人"的分水岭：**你随时得能喊停，而且停得干净。**

### 5.1 状态机

```
                 ┌─────────── 用户取消 / 预算超限 / 手动 ───────────┐
                 ▼                                                │
 queued ──▶ running ──┬──▶ waiting_confirm ──(用户确认)──▶ running
                      ├──▶ succeeded
                      ├──▶ failed ──(重试 ≤2 次)──▶ queued
                      └──▶ cancelling ──(到达安全点)──▶ cancelled

 进程重启 ──▶ interrupted ──(resume)──▶ running
```

状态与 `version` 存 `task` 表，**DB 是唯一真相源**，内存里的 Task 对象只是缓存。

这里刻意保留两个中间态：

- **`cancelling`**：已收到取消、尚未到安全点。没有它，你就分不清"要停"和"已停"。
- **`interrupted`**：非用户意图的暂停（进程重启、长任务让位）。可以 `resume`，不算失败。

### 5.2 取消的三条通道，各管一件事

| 通道 | 实现 | 管什么 |
|---|---|---|
| **① DB 状态** | `task.cancel_requested_at = now()` | **该不该停**——重启、多进程、事后审计都能看到 |
| **② 进程内信号** | `asyncio.Event` + Redis pub/sub | **多快停**——不用轮询，毫秒级唤醒 |
| **③ 框架中断** | `POST /sessions/{id}/interrupt`（AgentScope 内置） | **停得干不干净**——Agent 在安全点 unwind，会话仍可继续 |

为什么三条都要：单机时 ② 就够；进程一重启只有 ① 靠得住；而"干净退出、不留下半个 ReAct 循环"这件事，必须交给框架的 ③。AgentScope 的 interrupt 明确保证"运行中或 HITL 挂起的 run 都能从外部取消，agent 干净退出并准备好接收下一条输入"，正好是我们的落点。

```python
@app.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str):
    row = await db.fetchrow("""
        UPDATE app.task
           SET state = 'cancelling', cancel_requested_at = now(), version = version + 1
         WHERE id = $1 AND state IN ('queued','running','waiting_confirm')
        RETURNING id, session_id""", task_id)
    if row is None:
        raise HTTPException(409, "任务已结束或当前状态不可取消")
    await redis.publish(f"cancel:{task_id}", "1")                    # ② 低延迟
    await client.post(f"{SVC}/sessions/{row['session_id']}/interrupt")  # ③ 框架级
    return {"task_id": task_id, "state": "cancelling"}               # ≤1s 回执
```

### 5.3 安全点：只在这四个地方停

取消**不在工具执行中途硬杀**。`asyncio.Task.cancel()` 会把协程撕在任意一行，留下写了一半的文件、发了一半的请求。

四个安全检查点：

1. 调模型**之前**
2. 调工具**之前**
3. 工具返回**之后**（此时可能已产生副作用，走 §5.4）
4. 每轮 ReAct 结束、进入下一轮**之前**

```python
async def run_task(task_id: str):
    async with task_scope(task_id) as scope:        # 维护 Event + 子任务注册表
        agent = build_agent(task_id)                # 注入 model / tools / 权限
        async for ev in agent.reply_stream(msg):
            await scope.checkpoint()                # ← 安全点：必要时抛 TaskCancelled
            await persist_event(task_id, ev)
```

`checkpoint()` 里同时检查 ① DB 标记 和 ② Event，两个都看——Event 管快，DB 管准。

### 5.4 副作用与补偿

**能撤销的才叫"取消"，撤不掉的叫"纠正"。**

| 动作 | 幂等键 | 到安全点后的处理 |
|---|---|---|
| 建日程 | `cal:{task_id}:{hash}` | 补偿：删除事件 |
| 建草稿邮件 | `mail:{task_id}` | 补偿：删草稿 |
| **已发出邮件** | 同上 | **不可撤**：转"纠正"，提示发更正/撤回 |
| 写本地文件 | `fs:{task_id}:{path}` | 补偿：从快照恢复 |
| 记账 | `ledger:{task_id}` | 补偿：冲销分录 |
| 转账/支付 | — | L3 禁止，根本不会开始 |

幂等键写进 `tool_call` 表。重试、重复取消、进程恢复都不会造成两次扣款。

### 5.5 竞态与不可取消窗口

| 场景 | 处理 |
|---|---|
| 取消 vs 用户点"确认"同时到 | 乐观锁 `WHERE version = ?`，先到先赢；确认失败时回"抱歉，任务已取消" |
| 取消时正在等 HITL | 直接置 `cancelling`，确认按钮置灰，Agent 在下一安全点退出 |
| 工具已超时但无响应 | grace 30s 后强制断开，标 `cancel_timeout`，并提示**"该步骤可能已产生副作用，请核对"** |
| 取消后用户又后悔 | `cancelled` 不可 `resume`；但保留 `interrupted` 快照，可"从头重做" |

**取消的 SLA**：≤1s 给回执「正在停止…」；≤3s 到安全点；之后回「已停止 · 已完成 A、B，已撤销 C」。

### 5.6 级联

父任务取消 → 所有子任务（子代理、后台工具、RAG 索引任务）级联取消。实现上自维护一张 `parent_id` 注册表，配合 `asyncio.TaskGroup`，保证不留孤儿任务在后台烧钱。

---

## 6. 全链路日志：出事能复盘

### 6.1 Trace 模型

一次任务 = 一条 trace，`trace_id = task_id`。

```
task (trace)
 └─ step 1  intent        → llm_call (local/7b, 210ms)
 └─ step 2  rewrite       → llm_call (local/7b)  → rag.search(learn, k=5)
 └─ step 3  plan          → llm_call (ds-chat, 2.1s, 1.8k tok)
 └─ step 4  act           → tool_call(calendar.create) → permission(ASK) → 用户确认
 └─ step 5  act           → tool_call(mail.draft)  → error(timeout) → retry → ok
 └─ step 6  finalize      → llm_call (local/7b) → memory.write(work/xxx.md)
```

AgentScope 的 **Middleware 六个钩子**刚好对上这条链，不用改框架代码：

| 钩子 | 我们记什么 |
|---|---|
| `on_reply` | 任务起止、总耗时、最终输出、是否被取消 |
| `on_reasoning` | 每轮 ReAct 的上下文快照摘要、本轮打算做什么 |
| `on_model_call` | **完整 request/response**、token、TTFT、latency、`model_version` |
| `on_acting` | 工具名、参数摘要、返回码、耗时、幂等键、是否产生副作用 |
| `on_compress_context` | 压缩前后的 token 数、丢弃了什么（**上下文丢失事故的头号元凶**） |
| `on_system_prompt` | 最终系统提示的 hash + 长度（排查"提示被谁改了"） |

加上内置 **Tracing** 中间件（OTEL 格式），本地落 Postgres，需要时再接 Langfuse / Jaeger。

### 6.2 存哪：三层，别混

| 层 | 存什么 | 介质 | 保留 |
|---|---|---|---|
| 结构化 | token、耗时、状态、错误码、成本 | Postgres `llm_call` / `tool_call` | 90 天 |
| 大对象 | prompt 全文、工具原始返回、PDF/图片 | 对象存储 / 本地文件，DB 只存 `sha256 + 路径` | 30 天 |
| 聚合指标 | 成功率、P95、日成本、错误分布 | Postgres 物化视图 | 永久 |

**为什么不把 prompt 全文塞 DB**：单条 50KB，一天几百条，三个月就把库撑肥，备份和查询都被拖死。

### 6.3 `llm_call` 记录字段

```sql
CREATE TABLE app.llm_call (
  id                bigserial PRIMARY KEY,
  trace_id          uuid NOT NULL,
  task_id           uuid NOT NULL,
  step_no           int,
  role              text NOT NULL,   -- intent / rewrite / planner / subagent.work
  model_ref         text NOT NULL,   -- ds/deepseek-chat
  model_version     text NOT NULL,   -- 启动时快照，用于判断"是不是换模型导致的"
  prompt_tokens     int,
  completion_tokens int,
  cached_tokens     int,
  ttft_ms           int,
  latency_ms        int,
  retry             smallint DEFAULT 0,
  degraded          boolean DEFAULT false,   -- 是否走了 fallback
  status            text,            -- ok / error / timeout / cancelled
  error_class       text,            -- 见 §6.5
  cost_usd          numeric(10,6),
  request_blob      text,            -- sha256 -> 对象存储
  response_blob     text,
  created_at        timestamptz DEFAULT now()
);
CREATE INDEX ON app.llm_call (trace_id, step_no);
CREATE INDEX ON app.llm_call (created_at DESC) WHERE status <> 'ok';
```

### 6.4 复盘时要能回答四个问题

设计日志时只有这一个标准：**能不能回答这四个问题。**

| 问题 | 靠什么字段 |
|---|---|
| 它当时**看到了**什么？ | `request_blob`（完整上下文）+ `rag.search` 召回清单 + 命中的 `page_path` |
| 它**为什么**这么选？ | `plan` 输出 + `on_reasoning` 的本轮意图 + 候选工具列表 |
| 外部**真实发生了什么**？ | `tool_call.raw_result` + 错误码 + 是否被权限拒绝 |
| **多少钱、慢在哪**？ | `cost_usd` 汇总 + 最慢 span + `compress_context` 触发次数 |

### 6.5 异常复盘 Runbook（20 分钟内定位）

1. **定位**：用户说"昨天下午那个日程没建成" → 按 `chat_id + 时间窗` 查 `task` → 拿到 `task_id`
2. **看时间线**：`task_step` 找到**从哪一步开始歪**（不是最后一步）
3. **展开该步**：`llm_call.request_blob` → 上下文里有没有关键信息？召回是否漏了？
4. **对账**：模型说"已创建日程 8 月 3 日"，但 `tool_call` 返回 `404 calendar_id` → **幻觉**，责任在模型/工具契约
5. **归因打标**（自动 + 人工确认）：

   | 标签 | 含义 | 修哪里 |
   |---|---|---|
   | `retrieval_miss` | 该召回没召回 | wiki 描述、分块、embedding |
   | `context_overflow` | 压缩把关键信息压没了 | 压缩策略、库划分 |
   | `tool_contract` | 工具返回与模型预期不符 | 工具描述、返回值设计 |
   | `hallucinated_id` | 编造 ID / 不存在的事实 | 强约束提示 + 结果校验 |
   | `permission_blocked` | 该做的被权限挡了 | 规则太严 |
   | `timeout` | 外部慢 | 超时/重试/异步化 |
   | `model_drift` | 换了模型后变差 | 版本回滚对比 |

6. **产出两样东西**：一条**回归 case**（进评测集）+ 一条**教训**（写进 `ref/lessons`，下次规划时被检索到）

这一步很关键：**复盘不产出 case，第二次还会犯**。

### 6.6 回放三级

| 级别 | 做什么 | 用在哪 |
|---|---|---|
| **L1 读回放** | 按时间线重放事件流（只读） | 前端断线重连（AgentScope 的 SSE buffer 原生支持）、人工复盘 |
| **L2 重跑** | 用记录的输入重跑，**工具全部 mock** | 判断"是模型变了，还是代码变了" |
| **L3 回归** | case 固化为测试，CI 里跑 | 换模型/改提示前的守门 |

L2 是排查的王牌：同一份输入，老版本复现出错误、新版本正常 → 结论立刻清楚。

### 6.7 脱敏与保留

- 入库前正则脱敏：手机号、身份证、银行卡、`sk-*` 密钥、邮箱
- 需要原值排查 → 可逆加密，密钥单独存放，解密要二次确认并留痕
- 用户说"删掉这次对话" → 级联删 `llm_call/tool_call/blobs/wiki 引用`，**日志不能成为记忆删除的后门**
- 默认保留：结构化 90 天、大对象 30 天、聚合永久

---

## 7. 权限分级 ↔ AgentScope PermissionMode

| 我们的级别 | 判定 | AgentScope 落点 | 例子 |
|---|---|---|---|
| **L0 只读** | 不改状态 | `PermissionMode.EXPLORE` | 查日程、读邮件、搜 wiki |
| **L1 自动** | 可逆、无外部影响 | allow 规则（按工具+参数模式） | 建提醒、记笔记、加待办 |
| **L2 确认** | 有外部影响、难撤销 | `ASK`（DEFAULT 模式） | 发邮件、改日程、删文件 |
| **L3 禁止** | 涉钱、涉身份、不可逆 | deny 规则 + `bypass_immune` | 转账、改密码、代签 |

两条运维纪律：

1. **定时任务一律用 `DONT_ASK`**（无人值守时 ASK 会挂住）。此时所有 L2 动作自动降级为"生成待办，等你回家点确认"，而不是静默执行。
2. **生产环境永不 `BYPASS`**。它在框架里的定位就是"沙箱/完全信任"，我们的场景永远不是。

用户每次同意一个 ASK，可以顺手把它**固化成规则**（"以后这类日程直接建"）——AgentScope 的 suggested rule 机制原生支持，这是我们降低打扰的主要手段。

---

## 8. 部署

```
┌──────────────────────────────────────────────┐
│  docker compose                              │
│                                              │
│  web (FastAPI / agentscope create_app)       │
│    ├── 渠道适配（微信/TG）                    │
│    ├── Agent Service（会话/SSE/中断/定时）     │
│    └── 自定义 middleware（trace/成本/脱敏）    │
│                                              │
│  postgres:16 + pgvector   ← 唯一状态源        │
│  ollama                   ← 本地模型（可选托管）│
│  minio / 本地卷            ← 大对象（可选）     │
└──────────────────────────────────────────────┘
```

- **先用官方 `create_app`**：会话、SSE、后台任务、cron、中断都白送，别自己写一遍
- 自定义只从四个扩展点进：storage backend、vector store、model provider、middleware
- 只有需要自定义路由/多渠道聚合时，才在 `create_app` 外面再包一层 FastAPI

---

## 9. 落地路线

| 阶段 | 时间 | 做什么 | 验收 |
|---|---|---|---|
| **P0 打通** | 第 1 周 | `create_app` + Postgres + 1 个笔记工具 + wiki `me` 库 + trace 落库 | 能记下来，三天后问得出；任意一次调用能在 DB 查到完整 prompt |
| **P1 能停** | 第 2 周 | 日历/待办 + 早报（cron）+ **取消端到端** | 长任务中途喊停，3s 内停住且无残留 |
| **P2 敢用** | 第 3–4 周 | 写入类工具 + 权限映射 + 幂等补偿 + 模型路由 + 成本看板 | 发 10 封"草稿→确认"邮件零事故；单任务成本可见 |
| **P3 会想** | 第 2 月 | 五库成型 + 子代理 + 复盘 runbook + L3 回归集 | 每周主动提醒里 ≥60% 你觉得"提醒得对" |

顺序不能反：**先能记住 → 再能停下 → 再敢放权 → 最后谈主动**。跳过"能停"直接放权，第一次误操作就会让你把权限全关掉，然后它永远只是个聊天框。

---

## 10. 风险与对策

| 风险 | 表现 | 对策 |
|---|---|---|
| **取消后残留副作用** | 邮件发了、日程建了，界面上显示"已取消" | 幂等键 + 补偿动作 + 不可撤的明确告知 |
| **wiki 腐化** | 4000 页流水账，检索全是噪音 | 只写结论、强制合并、每周去重、低置信度进 inbox |
| **模型漂移** | 换了模型，行为悄悄变差 | `model_version` 落库 + L2 重跑 + L3 回归集 |
| **成本失控** | 一次多步任务烧掉几十万 token | 步数/token/金额三重上限 + 降级为半成品 |
| **记忆污染** | 三个月前的偏好覆盖了昨天说的 | 时间衰减 + 置信度 + 可纠正 + inbox 确认 |
| **日志成了隐私漏洞** | prompt 里有密码、病历 | 入库前脱敏 + 加密原值 + 删除可级联 |

---

## 附录：一张表看完关键选型

| 维度 | 选择 | 一句话理由 |
|---|---|---|
| 语言/框架 | Python + FastAPI + **AgentScope 2.0** | 引擎层白送，专注业务 |
| 数据库 | **PostgreSQL + pgvector**（不设中间态） | 一个库 = 一条真相，迁移一次就伤一次 |
| 向量 | `halfvec(1024)` + HNSW | 省一半内存，个人量级毫秒级召回 |
| 记忆 | **LLM wiki 多库**（me/work/learn/life/ref） | 知识是页面，不是碎块 |
| 取消 | DB 标记 + asyncio 信号 + `interrupt` | 该不该停 / 多快停 / 停得干不干净 |
| 模型 | 角色化 YAML + 本地/外部混跑 | 该省的地方省，该花的地方花 |
| 日志 | Middleware 六钩子 → Postgres + 对象存储 | 能回答"它当时看到了什么" |
