"""Postgres 连接池。

asyncpg 缺失或数据库连不上时，服务**仍然要能启动**（降级为内存模式），
否则新人 clone 下来第一件事就是 "跑不起来" —— 这是骨架最不该有的体验。
"""
from __future__ import annotations

import logging
from typing import Any

try:  # 允许在没有 asyncpg 的环境里跑单测
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


class Database:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 5) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self._pool: Any = None
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return self._pool is not None

    async def connect(self, timeout: float = 3.0) -> bool:
        if asyncpg is None:
            self.last_error = "asyncpg 未安装（pip install asyncpg）"
            log.warning("数据库不可用：%s，降级为内存模式", self.last_error)
            return False
        try:
            self._pool = await asyncpg.create_pool(
                self.dsn, min_size=self.min_size, max_size=self.max_size, timeout=timeout
            )
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT 1")
            log.info("数据库已连接")
            return True
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._pool = None
            log.warning("数据库不可用：%s，降级为内存模式", self.last_error)
            return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def execute(self, sql: str, *args: Any) -> str:
        if self._pool is None:
            raise RuntimeError("数据库未连接")
        async with self._pool.acquire() as conn:
            return await conn.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        if self._pool is None:
            raise RuntimeError("数据库未连接")
        async with self._pool.acquire() as conn:
            return await conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if self._pool is None:
            raise RuntimeError("数据库未连接")
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if self._pool is None:
            raise RuntimeError("数据库未连接")
        async with self._pool.acquire() as conn:
            return await conn.fetchval(sql, *args)
