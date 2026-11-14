"""FastAPI 入口。

启动顺序刻意做成"任何一环挂掉都不影响服务起来"：
  数据库连不上 → 内存模式；模型配置坏了 → echo 模式。
骨架的第一体验必须是 `uvicorn app.main:app` 一把就能打开 /docs。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from app.agent.factory import Runtime, build_runtime
from app.agent.routing import ModelRouter
from app.api.routes import router as api_router
from app.config import Settings, get_settings
from app.core.cancel import TaskRegistry
from app.core.runner import TaskRunner
from app.db.pool import Database
from app.db.repo import InMemoryTaskRepo, PostgresTaskRepo
from app.db.trace_sink import PostgresTraceSink

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    db: Database
    repo: Any
    registry: TaskRegistry
    router: Any
    runtime: Runtime
    runner: TaskRunner
    model_version: str


class _NullRouter:
    def all_roles(self) -> list[str]:
        return []

    def snapshot(self) -> dict[str, str]:
        return {}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(
            level=getattr(logging, settings.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        )

        db = Database(settings.database_url, settings.db_pool_min, settings.db_pool_max)
        await db.connect()
        repo = PostgresTaskRepo(db) if db.available else InMemoryTaskRepo()

        try:
            router = ModelRouter(settings.models_config_path, settings.force_local_on_sensitive)
            model_version = ",".join(f"{k}={v}" for k, v in router.snapshot().items())
        except Exception as e:
            log.error("模型配置加载失败（%s），路由退化为空", e)
            router = _NullRouter()
            model_version = "unknown"

        runtime = build_runtime(settings, router)  # type: ignore[arg-type]
        sink = PostgresTraceSink(db) if db.available else None
        runner = TaskRunner(repo, TaskRegistry(), runtime, settings, sink)

        app.state.ctx = AppContext(
            settings=settings,
            db=db,
            repo=repo,
            registry=runner.registry,
            router=router,
            runtime=runtime,
            runner=runner,
            model_version=model_version,
        )
        log.info(
            "启动完成 | db=%s | agent=%s | roles=%d",
            "postgres" if db.available else "memory",
            runtime.name,
            len(router.all_roles()),
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


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=True)
