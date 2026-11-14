# AssistantAgent

> 个人助理 Agent —— 学习 / 工作 / 生活，**一个入口，六层分工，五条铁律**。

![架构图](docs/architecture.png)

---

## 现在能跑什么

骨架的目标不是"功能多"，而是**三条最容易做坏的主干先立住**：

| 能力 | 状态 | 在哪 |
|---|---|---|
| FastAPI 服务 + 任务异步化 | ✅ 可用 | `app/api/routes.py` |
| 任务状态机（穷举迁移） | ✅ 可用 | `app/core/tasks.py` |
| **取消三通道 + 四个安全点** | ✅ 可用 | `app/core/cancel.py` |
| 副作用补偿（幂等键） | ✅ 可用 | `app/core/cancel.py`、`app/agent/tools.py` |
| 全链路 trace（含落库） | ✅ 可用 | `app/core/trace.py`、`app/db/trace_sink.py` |
| 角色化模型路由 + 敏感强制本地 | ✅ 可用 | `app/agent/routing.py`、`config/models.yaml` |
| **工具分层治理（profile → 库 → 工具）** | ✅ 可用 | `app/agent/toolbox.py`、`config/tools.yaml` |
| **记忆：LLM Wiki 多知识库** | ✅ 可用（文件存储） | `app/memory/`、`config/memory.yaml` |
| **HITL 确认闸门** | ✅ 可用 | `app/core/confirm.py` |
| Postgres schema（含 pgvector） | ✅ 可用 | `app/db/schema.sql` |
| Postgres 存储（wiki / 向量） | ✅ 已在真库验证 | `scripts/check_pg.py` |
| 离线评测（案例/跑批/计分卡） | ✅ 可用 | `eval/` |
| **AgentScope 接入** | ✅ 已对 2.0.11 校准 | [接入指南](docs/integration-agentscope.md) |

> 说明：AgentScope 适配层已在 2.0.11 上实测——`Agent(toolkit=)` 可构造，工具能装配进
> `Toolkit` 并真实调用。还差的只有「真实对话」这一步，需要本地 Ollama 或 API Key。
> 升级大版本后跑 `python scripts/check_agentscope.py` 重新对一遍。
> 其余代码有测试覆盖：`python -m unittest discover -s tests`。

---

## 30 秒跑起来

不需要数据库，不需要模型 —— 用 `echo` 运行时就能把「提交 → 执行 → 取消 → 留痕」整条链路走一遍：

```bash
pip install -e .

# 可选：没有 .env 也能跑，默认内存模式
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000/docs>，或直接试：

```bash
# 1) 提一个任务（立刻返回 task_id，不等模型）
curl -X POST localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"记一下：周四前把季度数据发给财务"}'
# → {"task_id":"...","state":"queued", ...}

# 2) 查结果
curl localhost:8000/api/tasks/<task_id>

# 3) 随时喊停（在下一个安全点生效，不是硬杀）
curl -X POST localhost:8000/api/tasks/<task_id>/cancel \
  -H "Content-Type: application/json" -d '{"reason":"不做了"}'
# → {"state":"cancelling","message":"已受理取消，将在下一个安全点停止"}

# 4) 复盘：这条任务到底发生了什么
curl localhost:8000/api/tasks/<task_id>/trace
```

### 带数据库

```bash
docker compose up -d db          # pgvector/pgvector:pg16，首次启动自动执行 schema.sql
cp .env.example .env             # 默认已指向 localhost:5432
uvicorn app.main:app --reload    # /health 里 db 会变成 up
```

> 手工初始化：`psql $DATABASE_URL -f app/db/schema.sql`
> schema 里的 `halfvec` 需要 pgvector ≥ 0.7；老版本把 `halfvec(1024)` 改成 `vector(1024)`。

### 接 AgentScope

```bash
pip install -e ".[agent]"
export AGENT_BACKEND=agentscope    # Windows: $env:AGENT_BACKEND="agentscope"
# 再按 config/models.yaml 配好 Ollama 或 API Key
```

`AGENT_BACKEND` 三档：`auto`（有就用，没有退 echo）/ `agentscope` / `echo`。

---

## 配置

**一处改，全项目生效。代码里没有硬编码的地址、预算和提示词。**

| 文件 | 管什么 |
|---|---|
| `config/app.yaml` | 端口、日志级别、数据库 DSN 与连接池、运行时后端、预算上限、子配置路径 |
| `config/models.yaml` | 角色 → 模型、降级链、敏感任务强制本地 |
| `config/tools.yaml` | 工具注册表：装哪些、副作用等级、权限等级、开不开 |
| `config/prompts/system.md` | 系统提示词，改行为不用改代码 |
| `.env` | **只有密钥**（API Key）和容器覆盖项，不进版本库 |

只有这 5 项允许被环境变量覆盖（`app/config.py: ENV_OVERRIDES`），其余一律以文件为准：

| 变量 | 覆盖 `app.yaml` 的 |
|---|---|
| `DATABASE_URL` | `database.dsn`（容器里用） |
| `AGENT_BACKEND` | `runtime.agent_backend` |
| `HOST` / `PORT` | `server.host` / `server.port` |
| `LOG_LEVEL` | `server.log_level` |

两个刻意的设计：

- **拼错字段名直接启动失败**（pydantic `extra="forbid"`）。宁可起不来，也别带着错配置静默运行——这类 bug 最难查。
- **密钥永远不进配置文件**。`models.yaml` 里只写 `api_key_ref: env:DEEPSEEK_API_KEY`，真值只存在于环境变量。

---

## 工具治理

工具不是一张平铺表，而是 **profile → 库 → 工具** 三层。一次任务只挂选中的库里的工具，
其余工具的 schema 根本不会进上下文。

```yaml
profiles:                       # 谁在用 → 能看哪些库
  work:     [calendar, mail, notes, task, research, system]
  learning: [notes, research, task, system]     # 学习教练看不到邮件库

libraries:                      # 能力域，权限在库这一层封顶
  - id: mail
    triggers: [邮件, 回信, 起草, 发给]
    max_permission: L2          # 本库封顶 L2
    tools:
      - name: mail.draft
        handler: mail_draft
        permission: L1          # 可以更保守，不能更激进
```

效果（本项目 7 库 11 工具）：

```
"把明天的产品评审挪到下午两点" → calendar        → 挂 2 个工具（不是 11 个）
"给张总回一封邮件"            → mail            → 挂 1 个
"嗯嗯"                       → notes, system   → 兜底 4 个
```

随时可以预览路由结果：

```bash
curl -G localhost:8000/api/tools/route --data-urlencode "q=明天的会挪到下午"
# {"libraries":["calendar"],"tools":["calendar.list_events","calendar.create_event"],...}
```

三条硬规则：**工具不能突破所在库的权限上限**、**工具全局不能重名**、**handler 必须存在**——
违反任何一条，启动直接失败。

设计细节（为什么路由不调模型、库内超过 15 个工具该怎么办）见[架构说明 §11](docs/architecture.md)。

---

## 记忆

知识不是"文档块"，而是**由 LLM 维护的 Markdown 页面**，按类型分五个库：

```
me     个人画像   偏好 / 作息 / 关系 / 目标      常驻注入，不靠检索
work   工作       项目、会议结论、承诺           按需检索
learn  学习       读书笔记、概念卡               按需检索
life   生活       健康、财务、家事               按需检索
ref    参考资料   剪藏、教训清单                 只读
```

检索和工具治理是同构的：**先定库，再定页，最后才取正文**。

```bash
curl "localhost:8000/api/memory/search?q=上次和张总聊的定价结论"
# kbs=["me","work"]  hits=[me/profile.md, work/pricing-2026-10.md]
```

三个关键约定：

1. **索引只有 name + description，没有正文**。50 条索引 ≈ 1.5k token，50 页正文 ≈ 25 万 token —— 这就是渐进式披露的全部意义。
2. **description 是给检索用的**。写成"关于 XX"等于没写，检索永远不会命中。
3. **写入能合并就合并**。定位到已有页面 → 追加小节 → 刷新索引；同一段内容不会追加第二次（幂等）。

存进去看一眼：

```bash
curl -X POST localhost:8000/api/memory/pages \
  -H "Content-Type: application/json" \
  -d '{"text":"张总的定价：阶梯定价 85 折，账期 60 天","kb":"work"}'
# {"action":"merged","kb":"work","path":"pricing-2026-10.md"}   ← 没有新建重复页
```

页面就是普通 Markdown（`data/wiki/<库>/<页>.md`），**用 Obsidian 打开就能改**。
换成 Postgres 存储只需在 `main.py` 换一行构造（`PostgresWikiStore`，同一接口）。

---

## 确认闸门（HITL）

L2 动作（发邮件、改日程、记账）在执行前会停下来等你点头。

```bash
# 1) 提一个会动手的请求
curl -X POST localhost:8000/api/chat -H "Content-Type: application/json" \
     -d '{"message":"记账：38 块打车"}'
# → 任务进入 waiting_confirm

# 2) 看看它在等什么（前端就渲染这个）
curl localhost:8000/api/confirmations
# {"count":1,"pending":[{"tool":"life.record_expense","permission":"L2","expires_in_s":299.4,...}]}

# 3) 答复
curl -X POST localhost:8000/api/tasks/<id>/confirm -H "Content-Type: application/json" \
     -d '{"approved":true}'
# {"message":"已同意，任务继续执行"}   → 任务从 waiting_confirm 回到 running
```

三个不显然的决定：

1. **暂停用进程内 future**。进程重启会丢，所以启动时把遗留的 `running` / `waiting_confirm`
   标成 `interrupted`，而不是假装它还在跑——一个骗人的 `running` 比一个诚实的 `interrupted` 危险得多。
2. **等待期间同时盯"答复"和"取消"**。少盯一个就出僵尸：点了取消，任务还在等你确认。
3. **超时按取消处理**（默认 300s），并落一条 `permission_event` 审计。
   "它到底问没问过我"是能查的。

---

## 目录

```
app/
├── main.py              # 入口：任何一环挂掉都能起（DB 挂→内存，模型坏→echo）
├── config.py            # 配置加载：config/ 目录是唯一真相
├── api/routes.py        # /health /api/chat /api/tasks/{id} [/cancel|/trace]
├── core/
│   ├── tasks.py         # 状态机：穷举所有合法迁移
│   ├── cancel.py        # 取消三通道 + 四个安全点 + 补偿
│   ├── confirm.py       # HITL 确认闸门：暂停、超时、取消竞速、重启清场
│   ├── runner.py        # 中枢：提交 → 执行 → 取消 → 落状态
│   └── trace.py         # 全链路事件
├── memory/
│   ├── wiki.py          # 页面模型：frontmatter / 索引 / 渐进式披露
│   ├── store.py         # 文件系统 / Postgres 两套存储，同一接口
│   ├── router.py        # 先定库再定页 + 写入合并与幂等
│   └── vector.py        # pgvector 索引（页面之上的一层）
├── agent/
│   ├── routing.py       # 角色 → 模型，敏感强制本地，降级链
│   ├── toolbox.py       # 工具分层治理：profile → 库 → 工具，路由与权限封顶
│   ├── factory.py       # echo / agentscope 两种运行时
│   └── tools.py         # 工具实现与契约（副作用 / 幂等键 / 权限）
└── db/
    ├── schema.sql       # Postgres 16 + pgvector
    ├── pool.py          # 连不上就降级，不阻塞启动
    ├── repo.py          # 内存 / Postgres 两套实现，乐观锁
    └── trace_sink.py    # trace 落库（失败不拖垮主流程）

config/
├── app.yaml             # 服务 / 数据库 / 运行时 / 预算 / 路径
├── models.yaml          # 角色 → 模型（进版本库、可 review）
├── tools.yaml           # 工具注册表：装哪些、什么权限、开不开
└── prompts/system.md    # 系统提示词，改行为不用改代码
eval/                    # 离线评测：cases / fixtures / runner / score
docs/                    # 架构说明、评测方案、架构图
deploy/                  # 远程 Postgres：compose + env + 隧道/备份说明
scripts/                 # smoke / check_agentscope / check_pg 三个验证脚本
tests/                   # 97 条 unittest
```

---

## 设计上的三个决定

### 1. `POST /api/chat` 立刻返回，不等着跑完

**只有异步化才可能取消。** 同步接口里，用户点"停"的那一瞬间，手上根本没有东西可停。

代价是客户端要轮询 `GET /api/tasks/{id}`（后续可换 SSE）。这个代价必须付。

### 2. 状态机单独成文件，且穷举所有迁移

个人助理的事故大多不是"算错"，是**状态说不清**：界面显示已取消，后台还在跑；或者已经发了邮件，状态还是 running。

所以 `ALLOWED` 表把迁移写死，`cancelling` / `interrupted` 两个中间态必须存在：
- 没有 `cancelling`，就分不清"要停"和"已停"
- 没有 `interrupted`，进程重启后只能算失败，其实可以 `resume`

### 3. 数据库连不上也要能启动

新人 `git clone` 之后第一件事是 `uvicorn`，不是装 Postgres。
所以 `Database.connect()` 失败 → 内存仓储；`ModelRouter` 报错 → echo 运行时。
**骨架的第一体验不该是"跑不起来"。**

---

## 测试

```bash
python -m unittest discover -s tests -v
```

97 条，覆盖：状态机穷举 / 非法迁移 / 四个安全点 / 补偿逆序 / 失败补偿留证 /
父子级联取消 / 真实任务中途取消 / 取消已结束任务返回冲突 / 路由敏感降级 / 配置加载与校验 / 工具库划分与路由 / 权限封顶 / 页面模型与索引 / 记忆检索与写入合并 / 确认闸门（同意·拒绝·超时·取消竞速·重启清场）/ HTTP 全链路。

不需要数据库、不需要模型、不需要网络。

想一眼看完主链路（工具路由 → 记忆检索 → 写入合并 → 对话 → 确认闸门）：

```bash
python scripts/smoke.py
```

---

## 评测

```bash
python eval/runner.py --agent scripted --out eval/report/demo
python eval/score.py  --run eval/report/demo/run.json
cat eval/report/demo/report.md
```

示例 case 里故意留了一次**越权发邮件**，所以你会看到 `BLOCK` —— 那是设计出来的：
安全项一票否决，一个 case 直接清零。细节见 `docs/eval-plan.md`。

---

## 路线图

- [x] **记忆层**：LLM Wiki 多库（me/work/learn/life/ref）+ 索引与渐进式披露
- [x] **HITL 全链路**：`waiting_confirm` → 确认/拒绝接口 → 审计
- [ ] **SSE 事件流**：替代轮询，断线可补历史
- [ ] **AgentScope 接入校准**：跑 `scripts/check_agentscope.py` 后对齐三个校准点
- [x] **Postgres 路径验证**：`scripts/check_pg.py` 7 步全过（任务/权限/wiki/pgvector）
- [ ] **权限规则固化**：用户同意过的动作变成 allow 规则，减少重复打扰
- [ ] **影子模式**：写工具 dry-run，只记录不执行

---

## 相关文档

- [架构说明](docs/architecture.md) —— 六层职责、取消逻辑、工具治理、记忆分层、日志与回放
- [评测方案](docs/eval-plan.md) —— 五层指标、三条铁律、计分卡与门禁
- [接入 AgentScope](docs/integration-agentscope.md) —— 四步 + 三个校准点 + 排查顺序
- [远程 Postgres 部署](deploy/README.md) —— SSH 隧道、备份恢复、安全检查清单
