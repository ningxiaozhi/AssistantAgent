-- ============================================================
-- 线上过程指标（跑在架构文档 §6 定义的 trace 表上）
-- 离线跑批看"会不会做对"，这里看"真实世界里做得怎么样"
--
-- 约定：表在 schema app，时间列均为 timestamptz
-- ============================================================

-- ① 每日任务概览：成功率、取消率、失败率
--    健康线：成功率 ≥ 85%，取消率 ≤ 10%（太高说明它太慢或太爱问）
SELECT
    date_trunc('day', created_at)                       AS day,
    count(*)                                            AS tasks,
    count(*) FILTER (WHERE state = 'succeeded')         AS succeeded,
    count(*) FILTER (WHERE state = 'cancelled')         AS cancelled,
    count(*) FILTER (WHERE state = 'failed')            AS failed,
    round(100.0 * count(*) FILTER (WHERE state = 'succeeded') / count(*), 1) AS ok_pct
FROM app.task
WHERE created_at > now() - interval '14 days'
GROUP BY 1
ORDER BY 1 DESC;

-- ② 工具失败率：哪个工具在拖后腿
--    健康线：单工具失败率 ≤ 5%
SELECT
    tool,
    count(*)                                                AS calls,
    count(*) FILTER (WHERE status = 'error')                AS errors,
    round(100.0 * count(*) FILTER (WHERE status = 'error') / count(*), 1) AS err_pct,
    round(avg(latency_ms))                                  AS avg_ms,
    round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)) AS p95_ms
FROM app.tool_call
WHERE created_at > now() - interval '7 days'
GROUP BY tool
ORDER BY err_pct DESC;

-- ③ 取消延迟：从点"停"到真的停住（架构里的 SLA 是 ≤3s）
--    超过 10s 的样本要逐条看，说明有工具没设超时
SELECT
    id,
    cancel_requested_at,
    updated_at,
    extract(epoch FROM (updated_at - cancel_requested_at)) AS cancel_latency_s
FROM app.task
WHERE state = 'cancelled' AND cancel_requested_at IS NOT NULL
  AND created_at > now() - interval '7 days'
ORDER BY cancel_latency_s DESC
LIMIT 50;

-- ④ 取消后残留副作用（目标：0 行）
--    已经出圈、撤不回来的动作，不允许出现在"已取消"的任务里
SELECT t.id, tc.tool, tc.args_digest, tc.created_at
FROM app.task t
JOIN app.tool_call tc ON tc.task_id = t.id
WHERE t.state = 'cancelled'
  AND tc.side_effect = 'external'
  AND tc.status = 'ok';

-- ⑤ 权限打扰率：每天问了你多少次、你同意了多少
--    健康线：≤3 次/天；同意率 >95% 说明规则太严（该固化成 allow rule 了）
SELECT
    date_trunc('day', created_at)               AS day,
    count(*)                                    AS asks,
    count(*) FILTER (WHERE decision = 'allowed') AS allowed,
    count(*) FILTER (WHERE decision = 'denied')  AS denied,
    round(100.0 * count(*) FILTER (WHERE decision = 'allowed') / count(*), 1) AS allow_pct
FROM app.permission_event
WHERE decision IN ('ask', 'allowed', 'denied')
  AND created_at > now() - interval '14 days'
GROUP BY 1
ORDER BY 1 DESC;

-- ⑥ 成本：钱花在哪个角色上（intent/planner/subagent.*）
--    用来回答"该不该把 planner 也换成便宜模型"
SELECT
    role,
    count(*)                                        AS calls,
    round(sum(cost_usd)::numeric, 4)                AS cost_usd,
    round(avg(cost_usd)::numeric, 5)                AS avg_cost,
    round(avg(latency_ms))                          AS avg_ms,
    round(100.0 * count(*) FILTER (WHERE degraded) / count(*), 1) AS degraded_pct
FROM app.llm_call
WHERE created_at > now() - interval '7 days'
GROUP BY role
ORDER BY cost_usd DESC;

-- ⑦ 上下文压缩：压得越狠，越容易丢关键信息
--    每次压缩都该在 trace 里留痕，出问题才能说"是压缩压没了"
SELECT
    date_trunc('day', created_at)     AS day,
    count(*)                          AS compressions,
    round(avg(prompt_tokens))         AS avg_before,
    round(avg(completion_tokens))     AS avg_after
FROM app.llm_call
WHERE role = 'compress_context'
  AND created_at > now() - interval '14 days'
GROUP BY 1
ORDER BY 1 DESC;

-- ⑧ 记忆健康度：新增了多少页、有多少孤儿页（无人引用 = 大概没被用过）
SELECT
    kb_key,
    count(*)                                              AS pages,
    count(*) FILTER (WHERE updated_at > now() - interval '7 days') AS touched_7d,
    count(*) FILTER (WHERE updated_at < now() - interval '180 days') AS stale_180d
FROM app.wiki_page
GROUP BY kb_key
ORDER BY pages DESC;

-- ⑨ 幻觉代理指标：工具报错之后，这一步仍然产出了写操作
--    真正的判定要人工/对账，这条只是把可疑任务筛出来
WITH failed AS (
    SELECT task_id, step_no
    FROM app.tool_call
    WHERE status = 'error' AND created_at > now() - interval '7 days'
)
SELECT t.id, t.state, count(*) AS suspicious_steps
FROM app.task t
JOIN failed f ON f.task_id = t.id
JOIN app.tool_call tc
  ON tc.task_id = f.task_id
 AND tc.step_no = f.step_no
 AND tc.side_effect IN ('reversible', 'external')
 AND tc.status = 'ok'
GROUP BY t.id, t.state
ORDER BY suspicious_steps DESC;
