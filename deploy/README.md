# 远程 Postgres（pgvector）：一条命令起，一条命令验

> 场景：应用跑在你本地，数据库跑在另一台机器（NAS / 云主机 / 家里的旧笔记本）的 Docker 里。
> 这个目录只解决两件事：**怎么安全地把库跑起来**、**怎么确认它真的能用**。

---

## 1. 在远端机器上起库

```bash
# 远端
mkdir -p ~/assistant && cd ~/assistant
# 把仓库的 deploy/ 和 app/db/schema.sql 传上来（或直接 git clone）
cp deploy/.env.example deploy/.env
# 改 deploy/.env 里的 POSTGRES_PASSWORD，别用默认值
docker compose -f deploy/docker-compose.remote.yml --env-file deploy/.env up -d
docker compose -f deploy/docker-compose.remote.yml logs -f db   # 看到 "database system is ready"
```

首次启动会自动执行 `app/db/schema.sql`（挂到 `/docker-entrypoint-initdb.d/`）。
**注意**：initdb 脚本只在数据卷为空时执行一次。卷里已有数据时改 schema 要手工执行：

```bash
docker exec -i assistant-db psql -U assistant -d assistant < app/db/schema.sql
```

---

## 2. 端口只绑回环，别开 0.0.0.0

`docker-compose.remote.yml` 里写的是：

```yaml
ports:
  - "127.0.0.1:5432:5432"
```

意思是**只有远端机器自己**能连 5432。你从笔记本访问要走 SSH 隧道：

```bash
# 本地
ssh -N -L 5433:127.0.0.1:5432 user@<远端IP>
# 之后本地 127.0.0.1:5433 就等于远端的 5432
```

然后本地 `.env`：

```bash
DATABASE_URL=postgresql://assistant:<密码>@127.0.0.1:5433/assistant
```

**为什么不用 `0.0.0.0:5432`**：公网上的 5432 每天被扫几十万次，一个弱密码就是一台挖矿机。
真要直连，至少配防火墙白名单 + `pg_hba.conf` 限制来源网段，别裸奔。

有 Tailscale/WireGuard 的话更好：直接走内网 IP，不用隧道，也不用开公网端口。

---

## 3. 验一遍（**别跳过**）

在**本地**（能连到库的那一端）：

```bash
export DATABASE_URL="postgresql://assistant:密码@127.0.0.1:5433/assistant"
pip install asyncpg
python scripts/check_pg.py
```

它会依次验证 7 件事，最后打印 PASS/FAIL：

```
① 连接                ✅ 12ms
② 应用 schema          ✅ 38ms（幂等，可重复跑）
③ 任务仓储 roundtrip   ✅ 任务创建→迁移→读回，乐观锁生效
④ 权限事件落库         ✅
⑤ wiki 页面写入/读取    ✅
⑥ pgvector 建索引+检索  ✅ 3 块，命中 page_path=smoke/check.md
⑦ 清理                 ✅
```

**这一步把仓库里标注"未在真库验证"的两个实现（`PostgresWikiStore` / `PgVectorStore`）真正跑了一遍。**
任何一步 ❌，输出里会写清楚是哪条 SQL 或哪个假设错了。

---

## 4. 切存储

验证通过后，`app/main.py` 里已经按 `db.available` 自动选择：

```python
wiki_store = PostgresWikiStore(db) if db.available else LocalWikiStore(...)
```

所以只要 `DATABASE_URL` 连得上，wiki 和向量就自动走 Postgres，**不用改代码**。

---

## 排错：`permission denied for database` / `must be owner of table`

应用用户不是**库属主**时，会出现两连击：

1. `permission denied for database` —— 连 `app` schema 都建不了
2. 早期用别的用户建过表，新用户**能看见表名但改不了** —— `must be owner of table`

第二个尤其容易误判：`information_schema` 会按权限过滤，所以你可能看到"没有表"，
实际上是"有表但你没权限看"。

一条命令根治（用超级用户跑一次，只转移 `app` schema 内对象的所有权，不动数据）：

```bash
docker exec -i <容器> psql -U postgres -d assistant < deploy/init-grants.sql
# 记得把文件开头的 dbuser 改成你的应用用户名
```

脚本做四件事：装 pgvector → 给库级 CREATE → **把 app schema 及内部表/序列属主转给应用用户**
→ 再验证一遍权限。

跑完重跑 `python scripts/check_pg.py`，第 ① 步输出里会显示 `can_create` 是否已变成 True。

---

## 排错：`extension "vector" is not available`

这条报错 100% 说明**跑的是纯 postgres 镜像，不是 pgvector 镜像**。
`check_pg.py` 会降级继续验非向量路径，并在第 ⑥ 步跳过。

两种修法，选一个：

**A. 换镜像（推荐，数据不丢）**

pgvector 镜像是 `postgres:16` + 扩展，同一个 PGDATA 目录可以直接复用：

```bash
# 远端，改 compose 里的 image
sed -i 's|image: postgres:16|image: pgvector/pgvector:pg16|' docker-compose.yml
docker compose up -d            # 会用新镜像重建容器，卷不变
docker exec assistant-db psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**B. 往现有容器里装扩展（不用重建，但容器重建后会丢）**

```bash
docker exec -it <容器名> bash -c "apt-get update && apt-get install -y postgresql-16-pgvector"
docker exec <容器名> psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

装完重跑 `python scripts/check_pg.py`，第 ⑥ 步应该变成 ✅。

---

## 5. 备份（个人数据，别偷懒）

```bash
# 备份
docker exec assistant-db pg_dump -U assistant -d assistant -Fc > backup-$(date +%F).dump

# 恢复
docker exec -i assistant-db pg_restore -U assistant -d assistant --clean < backup-2026-11-14.dump
```

放进 crontab，保留 8 周：

```cron
0 3 * * * docker exec assistant-db pg_dump -U assistant -d assistant -Fc | gzip > ~/backup/assistant-$(date +\%F).dump.gz
```

`pg_dump` 一份 = 记忆 + 日志 + 任务 + 向量全都有。恢复时 `CREATE EXTENSION vector`
需要 pgvector 扩展存在，所以**用同一个镜像恢复**，别换 postgres 官方镜像。

---

## 6. 安全检查清单

- [ ] `POSTGRES_PASSWORD` 是随机生成的强密码（不是 `assistant`）
- [ ] 端口绑定是 `127.0.0.1:5432`，不是 `0.0.0.0:5432`
- [ ] 远程访问走 SSH 隧道或 VPN，不是公网直连
- [ ] `pgdata` 是具名卷或宿主机目录，不是容器内临时层
- [ ] 备份在跑，且**恢复过一次**（没恢复过的备份等于没有）
- [ ] 数据库磁盘有余量：向量索引会占空间，100 万块 × 1024 维 ≈ 2GB

---

## 文件说明

| 文件 | 用途 |
|---|---|
| `docker-compose.remote.yml` | 远端起库（pgvector/pg16 + 健康检查 + 只绑回环） |
| `.env.example` | 密码等环境变量模板 |
| `../scripts/check_pg.py` | 7 步验证脚本，把 Postgres 路径真正跑一遍 |
| `../app/db/schema.sql` | 表结构，initdb 时自动执行 |
