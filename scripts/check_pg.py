"""Postgres 全路径验证：把仓库里"未在真库验证"的部分真正跑一遍。

    export DATABASE_URL="postgresql://assistant:密码@127.0.0.1:5433/assistant"
    python scripts/check_pg.py

7 步：连接 → 建表 → 任务仓储 → 权限事件 → wiki 页面 → pgvector 检索 → 清理。
任何一步 ❌，输出会告诉你错在哪条 SQL、哪个假设。

在生产库上跑也没关系：只写 `smoke/check.md` 和一条临时任务，最后会删干净。
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db.pool import Database  # noqa: E402
from app.db.repo import PostgresTaskRepo  # noqa: E402
from app.memory.store import PostgresWikiStore  # noqa: E402
from app.memory.vector import HashEmbedder, PgVectorStore  # noqa: E402
from app.memory.wiki import WikiPage  # noqa: E402
from app.core.tasks import TaskState  # noqa: E402

OK, BAD = "✅", "❌"
KB = "smoke"
PAGE = "check.md"


def _dsn() -> str:
    import os

    from app.config import AppConfig

    return os.environ.get("DATABASE_URL") or AppConfig.load().database.dsn


def step(name: str):
    """计时装饰器：每一步打印耗时，慢在哪一眼能看到。"""

    def deco(fn):
        async def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                detail = await fn(*args, **kwargs)
                ms = (time.perf_counter() - t0) * 1000
                print(f"{OK} {name:<22} {ms:6.0f}ms  {detail or ''}")
                return True
            except Exception as e:
                ms = (time.perf_counter() - t0) * 1000
                print(f"{BAD} {name:<22} {ms:6.0f}ms  {type(e).__name__}: {e}")
                return False

        return wrapper

    return deco


async def main() -> int:
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        print(f"{BAD} 未安装 asyncpg：pip install asyncpg")
        return 1

    dsn = _dsn()
    safe = dsn.split("@")[-1] if "@" in dsn else dsn
    print(f"目标：…@{safe}\n")

    db = Database(dsn, min_size=1, max_size=2)
    if not await db.connect(timeout=8.0):
        print(f"{BAD} 连接                {db.last_error}")
        return 1

    ok = True
    vector_ok = True
    task_id: str | None = None
    repo = PostgresTaskRepo(db)
    wiki = PostgresWikiStore(db)
    vectors = PgVectorStore(db, HashEmbedder(dim=1024))

    @step("① 连接")
    async def _connect():
        ver = await db.fetchval("select version()")
        return ver.split(",")[0]

    # 连接后先做权限体检：没有 CREATE 权限的话，下面每一步都会以
    # "permission denied" 收场，而那句话不告诉你该找谁要权限。
    who = await db.fetchrow(
        """
        select current_user as usr,
               (select rolsuper from pg_roles where rolname = current_user) as is_super,
               pg_get_userbyid((select datdba from pg_database where datname = current_database())) as owner,
               has_database_privilege(current_user, current_database(), 'CREATE') as can_create
        """
    )
    if not who["can_create"]:
        print(f"\n{BAD} 当前用户 {who['usr']} 对库 {_dsn().rsplit('/', 1)[-1]} 没有 CREATE 权限")
        print(f"   库属主是 {who['owner']}，超管={who['is_super']} → 建不了 app schema，也装不了扩展。")
        print("\n   用超级用户执行下面这句（二选一）：")
        print(f"     GRANT CREATE ON DATABASE {_dsn().rsplit('/', 1)[-1]} TO {who['usr']};")
        print(f"     ALTER DATABASE {_dsn().rsplit('/', 1)[-1]} OWNER TO {who['usr']};")
        print("   容器里执行：")
        print(f"     docker exec -i <容器> psql -U postgres -d <库> < deploy/init-grants.sql")
        await db.close()
        return 1
    print(f"   用户 {who['usr']}（库属主 {who['owner']}，超管 {who['is_super']}）")


    @step("② 应用 schema")
    async def _schema():
        nonlocal vector_ok
        sql = (ROOT / "app" / "db" / "schema.sql").read_text(encoding="utf-8")
        try:
            await db.execute(sql)
            vector_ok = True
        except Exception as e:
            # 没有 pgvector 时不要整盘放弃：去掉向量相关语句，把其余表先建起来，
            # 这样至少能验证任务/权限/wiki 三条路径是否正常。
            msg = str(e)
            if "vector" not in msg.lower():
                raise
            reduced = "\n".join(
                s + ";" for s in sql.split(";")
                if s.strip()
                and not any(k in s.lower() for k in ("vector", "halfvec", "hnsw", "kb_chunk"))
            )
            await db.execute(reduced)
            vector_ok = False
            print(f"   ⚠️  当前实例没有 pgvector，已跳过 kb_chunk。换成 pgvector/pgvector:pg16 镜像后重跑。")
        tables = await db.fetchval(
            "select count(*) from information_schema.tables where table_schema='app'"
        )
        return f"app schema 下 {tables} 张表"

    @step("③ 任务仓储 roundtrip")
    async def _task():
        nonlocal task_id
        row = await repo.create(input="check_pg", channel="smoke")
        task_id = row["id"]
        await repo.transition(task_id, TaskState.RUNNING)
        done = await repo.transition(
            task_id, TaskState.SUCCEEDED, expect_version=2, output="ok"
        )
        got = await repo.get(task_id)
        assert got and got["state"] == "succeeded", got
        return f"version={done['version']} 乐观锁生效"

    @step("④ 权限事件落库")
    async def _perm():
        await repo.record_permission(task_id, "life.record_expense", "ask", "L2")
        await repo.record_permission(task_id, "life.record_expense", "allowed", "user")
        n = await db.fetchval(
            "select count(*) from app.permission_event where task_id=$1", uuid.UUID(task_id)
        )
        return f"{n} 条审计记录"

    @step("⑤ wiki 页面写入/读取")
    async def _wiki():
        page = WikiPage.new(
            KB, PAGE, "检页", "验证 PostgresWikiStore 的读写", "## 内容\n这是验证页。", tags=["smoke"]
        )
        await wiki.put_page(page)
        got = await wiki.get_page(KB, PAGE)
        assert got is not None and "验证页" in got.body, got
        pages = await wiki.list_pages(KB)
        return f"{len(pages)} 页"

    @step("⑥ pgvector 建索引 + 检索")
    async def _vector():
        text = "阶梯定价 85 折，账期 60 天，客户张总"
        await vectors.index_page(KB, PAGE, text)
        hits = await vectors.search(KB, "阶梯定价 账期", top_k=3)
        assert hits, "检索结果为空"
        assert hits[0].page_path == PAGE, f"命中了 {hits[0].page_path}"
        return f"{len(hits)} 块，top1={hits[0].page_path} score={hits[0].score:.3f}"

    @step("⑦ 清理")
    async def _cleanup():
        try:
            await vectors.delete_page(KB, PAGE)
        except Exception:
            pass  # 没有 kb_chunk 表（未装 pgvector）时忽略
        await wiki.delete_page(KB, PAGE)
        if task_id:
            await db.execute("delete from app.task where id=$1", uuid.UUID(task_id))
        return "已删除测试数据"

    for fn in (_connect, _schema, _task, _perm, _wiki):
        ok = await fn() and ok
        if not ok:
            # 失败了也尽量把残留清掉
            try:
                if task_id:
                    await db.execute("delete from app.task where id=$1", uuid.UUID(task_id))
                await db.execute("delete from app.wiki_page where kb_key=$1", KB)
            except Exception:
                pass
            break

    if ok and vector_ok:
        ok = await _vector() and ok
    elif ok:
        print(f"⏭️  ⑥ pgvector 建索引 + 检索  跳过（该实例未安装 vector 扩展）")

    if ok:
        await _cleanup()

    await db.close()
    print()
    if ok and vector_ok:
        print(f"{OK} 全部通过：Postgres 路径可用，wiki 与向量会自动切到数据库。")
        return 0
    if ok:
        print(f"⚠️  非向量路径全部通过。还差一步：把镜像换成 pgvector/pgvector:pg16 后重跑。")
        return 2
    print(f"{BAD} 有步骤失败，按上面的报错修。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
