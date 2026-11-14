"""FastAPI 入口。

启动顺序刻意做成"任何一环挂掉都不影响服务起来"：
  数据库连不上 → 内存模式；模型配置坏了 → echo 模式。
骨架的第一体验必须是 `uvicorn app.main:app` 一把就能打开 /docs。

所有配置来自 config/ 目录（见 app/config.py），代码里不再有硬编码的地址和预算。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from app.agent.factory import Runtime, build_runtime
from app.agent.routing import ModelRouter
from app.agent.toolbox import ToolCatalog, ToolRouter
from app.api.routes import router as api_router
from app.config import ROOT, AppConfig, ConfigError
from app.core.cancel import TaskRegistry
from app.core.confirm import PermissionGate, recover_stale_tasks
from app.core.runner import TaskRunner
from app.db.pool import Database
from app.db.repo import InMemoryTaskRepo, PostgresTaskRepo
from app.db.trace_sink import PostgresTraceSink
from app.memory import LocalWikiStore, MemoryConfig, MemoryRouter, MemoryWriter
from app.memory.store import PostgresWikiStore

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    config: AppConfig
    db: Database
    repo: Any
    registry: TaskRegistry
    router: Any
    runtime: Runtime
    runner: TaskRunner
    model_version: str
    catalog: ToolCatalog | None = None
    tool_router: ToolRouter | None = None
    memory: MemoryRouter | None = None
    memory_writer: MemoryWriter | None = None
    wiki_store: Any = None
    gate: PermissionGate | None = None


class _NullRouter:
    def all_roles(self) -> list[str]:
        return []

    def snapshot(self) -> dict[str, str]:
        return {}


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or AppConfig.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=getattr(logging, cfg.server.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        )

        db = Database(cfg.database.dsn, cfg.database.pool_min, cfg.database.pool_max)
        await db.connect(timeout=cfg.database.connect_timeout_s)
        repo = PostgresTaskRepo(db) if db.available else InMemoryTaskRepo()

        # 上次进程留下的 running / waiting_confirm 不会有协程在跑，标成 interrupted
        try:
            await recover_stale_tasks(repo)
        except Exception as e:
            log.warning("遗留任务清理失败: %s", e)

        gate = PermissionGate(repo, cfg.hitl.timeout_s)

        try:
            router = ModelRouter(cfg.models_path, cfg.runtime.force_local_on_sensitive)
            model_version = ",".join(f"{k}={v}" for k, v in router.snapshot().items())
        except Exception as e:
            log.error("模型配置加载失败（%s），路由退化为空", e)
            router = _NullRouter()
            model_version = "unknown"

        # 工具分层治理：库没装好不影响服务起来，只是这次没有工具可用
        catalog: ToolCatalog | None = None
        try:
            catalog = ToolCatalog.load(cfg.tools_path)
        except Exception as e:
            log.error("工具配置加载失败（%s），本次运行不挂载工具", e)

        # 记忆层：wiki 多库。库没装好也不影响服务起来
        memory_cfg: MemoryConfig | None = None
        memory_router: MemoryRouter | None = None
        memory_writer: MemoryWriter | None = None
        wiki_store: Any = None
        try:
            memory_cfg = MemoryConfig.load(cfg.memory_path)
            if db.available:
                wiki_store = PostgresWikiStore(db)
            else:
                seed = Path(memory_cfg.defaults.seed_dir)
                if not seed.is_absolute():
                    seed = ROOT / seed
                wiki_store = LocalWikiStore(ROOT / "data" / "wiki", seed_dir=seed)
            memory_router = MemoryRouter(memory_cfg, wiki_store)
            memory_writer = MemoryWriter(memory_cfg, wiki_store)
        except Exception as e:
            log.error("记忆配置加载失败（%s），本次运行不挂载记忆", e)

        runtime = build_runtime(
            cfg,
            router,  # type: ignore[arg-type]
            catalog,
            memory_router,
            gate,
            cfg.hitl.demo_in_echo,
        )
        sink = PostgresTraceSink(db) if db.available else None
        runner = TaskRunner(repo, TaskRegistry(), runtime, cfg, sink)

        app.state.ctx = AppContext(
            config=cfg,
            db=db,
            repo=repo,
            registry=runner.registry,
            router=router,
            runtime=runtime,
            runner=runner,
            model_version=model_version,
            catalog=catalog,
            tool_router=ToolRouter(catalog) if catalog else None,
            memory=memory_router,
            memory_writer=memory_writer,
            wiki_store=wiki_store,
            gate=gate,
        )
        log.info(
            "启动完成 | config=%s | db=%s | agent=%s | roles=%d | tools=%s | memory=%s",
            cfg.config_dir,
            "postgres" if db.available else "memory",
            runtime.name,
            len(router.all_roles()),
            f"{catalog.stats()['libraries']}库/{catalog.stats()['tools']}工具" if catalog else "none",
            f"{len(memory_cfg.libraries)}库" if memory_cfg else "none",
        )
        try:
            yield
        finally:
            await runner.shutdown()
            await db.close()

    app = FastAPI(
        title="AssistantAgent",
        description="个人助理 Agent：学习 / 工作 / 生活",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app


try:
    app = create_app()
except ConfigError as _e:  # 配置缺失时不要 import 就炸，交给启动日志说清楚
    log.error("配置加载失败: %s", _e)
    raise


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    _cfg = AppConfig.load()
    uvicorn.run("app.main:app", host=_cfg.server.host, port=_cfg.server.port, reload=True)
