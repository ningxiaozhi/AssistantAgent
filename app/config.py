"""应用配置：环境变量 > .env > 默认值。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 数据库
    database_url: str = "postgresql://assistant:assistant@127.0.0.1:5432/assistant"
    db_pool_min: int = 1
    db_pool_max: int = 5

    # Agent
    agent_backend: str = "auto"  # auto | agentscope | echo
    models_config: str = "config/models.yaml"
    force_local_on_sensitive: bool = True

    # 模型
    ollama_base_url: str = "http://127.0.0.1:11434"
    deepseek_api_key: str | None = None
    dashscope_api_key: str | None = None

    # 预算（单任务）
    max_steps: int = 8
    max_tokens: int = 120_000
    max_usd_per_task: float = 0.15

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    @property
    def models_config_path(self) -> Path:
        p = Path(self.models_config)
        return p if p.is_absolute() else ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
