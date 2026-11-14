"""配置加载：`config/` 目录是唯一来源。

三层，各管各的：
    1. `config/app.yaml`  非敏感配置的唯一真相（进版本库，可 review、可 diff）
    2. `.env`             只有密钥（API Key），不进版本库
    3. 环境变量           覆盖少数几项（DATABASE_URL / AGENT_BACKEND / HOST / PORT / LOG_LEVEL）

为什么不再用一堆环境变量：
    环境变量看不见全貌、改一项要改三处（compose、.env、启动脚本），
    而且"当前到底跑的是哪套配置"永远说不清。配置文件能 diff，这是它最大的价值。

拼错字段名会直接启动失败（extra="forbid"）—— 宁可起不来，也别带着错配置静默运行。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = ROOT / "config"


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- 分段配置
class _Strict(BaseModel):
    """未知字段直接报错：配置文件拼错一个字母，应该立刻炸，而不是静默用默认值。"""

    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"


class DatabaseConfig(_Strict):
    dsn: str
    pool_min: int = 1
    pool_max: int = 5
    connect_timeout_s: float = 3.0


class RuntimeConfig(_Strict):
    agent_backend: str = "auto"
    default_role: str = "planner"
    force_local_on_sensitive: bool = True


class LimitsConfig(_Strict):
    max_steps: int = 8
    max_tokens: int = 120_000
    max_usd_per_task: float = 0.15


class PathsConfig(_Strict):
    models: str = "models.yaml"
    tools: str = "tools.yaml"
    memory: str = "memory.yaml"
    prompts: str = "prompts"


class AppConfig(_Strict):
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    config_dir: Path = DEFAULT_CONFIG_DIR

    # ---------- 派生的绝对路径 ----------
    @property
    def models_path(self) -> Path:
        return self.config_dir / self.paths.models

    @property
    def tools_path(self) -> Path:
        return self.config_dir / self.paths.tools

    @property
    def memory_path(self) -> Path:
        return self.config_dir / self.paths.memory

    @property
    def prompts_dir(self) -> Path:
        return self.config_dir / self.paths.prompts

    def read_prompt(self, name: str = "system.md", default: str = "") -> str:
        p = self.prompts_dir / name
        return p.read_text(encoding="utf-8").strip() if p.exists() else default

    # ---------- 载入 ----------
    @classmethod
    def load(
        cls,
        config_dir: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "AppConfig":
        if env is None:
            _load_dotenv(ROOT / ".env")
            env = os.environ
        env = dict(env)

        raw_dir = config_dir or env.get("ASSISTANT_CONFIG_DIR") or DEFAULT_CONFIG_DIR
        cdir = Path(raw_dir)
        if not cdir.is_absolute():
            cdir = (ROOT / cdir).resolve()

        app_file = cdir / "app.yaml"
        if not app_file.exists():
            raise ConfigError(f"缺少配置文件: {app_file}")
        try:
            raw = yaml.safe_load(app_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"YAML 解析失败 {app_file}: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigError(f"{app_file} 顶层必须是映射（key: value）")

        _apply_env_overrides(raw, env)

        try:
            return cls(config_dir=cdir, **raw)
        except ValidationError as e:
            raise ConfigError(f"配置校验失败 {app_file}:\n{e}") from e


# ---------------------------------------------------------------- 覆盖与 dotenv
# 只允许这几项被环境变量覆盖，避免"配置有两套真相"
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "HOST": ("server", "host"),
    "PORT": ("server", "port"),
    "LOG_LEVEL": ("server", "log_level"),
    "DATABASE_URL": ("database", "dsn"),
    "AGENT_BACKEND": ("runtime", "agent_backend"),
}


def _apply_env_overrides(raw: dict[str, Any], env: Mapping[str, str]) -> None:
    for env_key, (section, field) in ENV_OVERRIDES.items():
        value = env.get(env_key)
        if value is None or value == "":
            continue
        raw.setdefault(section, {})
        if not isinstance(raw[section], dict):
            raise ConfigError(f"app.yaml 里的 {section} 必须是映射，无法被 {env_key} 覆盖")
        raw[section][field] = value


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：只认 KEY=VALUE，不覆盖已存在的环境变量。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
