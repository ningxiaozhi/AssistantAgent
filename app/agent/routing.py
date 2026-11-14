"""角色化模型路由。

设计要点（架构文档 §2）：
  1. 角色决定模型：intent 用快模型，planner 用强模型，各子代理各用各的
  2. 敏感任务强制本地 —— 由路由层判定，不信模型自报
  3. 有降级链 —— 主模型 → fallback → 本地兜底，降级必须打标
  4. 运行中的任务不热切换模型，配置变更只对新任务生效
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ModelSpec:
    ref: str  # "local/qwen2.5-7b-instruct"
    provider: str
    model: str
    temperature: float = 0.3
    max_tokens: int | None = None
    base_url: str | None = None
    api_key: str | None = None
    fallback: tuple[str, ...] = ()


@dataclass
class Resolved:
    role: str
    spec: ModelSpec
    degraded: bool = False
    reason: str = ""
    chain: tuple[ModelSpec, ...] = field(default_factory=tuple)


def _split_ref(ref: str) -> tuple[str, str]:
    if "/" not in ref:
        raise ConfigError(f"模型引用必须写成 provider/model，收到: {ref}")
    provider, model = ref.split("/", 1)
    return provider.strip(), model.strip()


class ModelRouter:
    def __init__(self, config_path: str | Path, force_local_on_sensitive: bool = True) -> None:
        self.config_path = Path(config_path)
        self.force_local = force_local_on_sensitive
        self.providers: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        self.load()

    # ---------- 载入 ----------
    def load(self) -> None:
        if not self.config_path.exists():
            raise ConfigError(f"模型配置不存在: {self.config_path}")
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        self.providers = raw.get("providers", {})
        self.roles = raw.get("roles", {})
        if not self.roles:
            raise ConfigError("models.yaml 里没有任何 roles")
        for name, role in self.roles.items():
            provider, _ = _split_ref(role["model"])
            if provider not in self.providers:
                raise ConfigError(f"角色 {name} 引用了未定义的 provider: {provider}")

    # ---------- 构造 ----------
    def _spec(self, ref: str, **overrides: Any) -> ModelSpec:
        provider, model = _split_ref(ref)
        pconf = self.providers.get(provider, {})
        api_key_ref = pconf.get("api_key_ref")
        api_key = os.environ.get(api_key_ref.split(":", 1)[1]) if api_key_ref else None
        return ModelSpec(
            ref=ref,
            provider=provider,
            model=overrides.pop("model_name", model),
            temperature=float(overrides.get("temperature", 0.3)),
            max_tokens=overrides.get("max_tokens"),
            base_url=pconf.get("base_url"),
            api_key=api_key,
            fallback=tuple(overrides.get("fallback", ())),
        )

    def local_ref_for(self, role: str) -> str | None:
        """某个角色的本地兜底：优先用角色自己声明的 local_model，否则用 intent 的模型。"""
        role_conf = self.roles.get(role, {})
        if "local_model" in role_conf:
            return role_conf["local_model"]
        intent = self.roles.get("intent", {})
        ref = intent.get("model")
        if ref and _split_ref(ref)[0] == "local":
            return ref
        for p in self.providers:
            if p == "local":
                return f"local/{intent.get('fallback_model', 'qwen2.5-7b-instruct')}"
        return None

    # ---------- 解析 ----------
    def resolve(self, role: str, sensitive: bool = False) -> Resolved:
        if role not in self.roles:
            raise ConfigError(f"未定义的角色: {role}")
        conf = self.roles[role]
        primary = self._spec(conf["model"], **conf)

        if sensitive and self.force_local and primary.provider != "local":
            local_ref = self.local_ref_for(role)
            if local_ref:
                spec = self._spec(local_ref, temperature=conf.get("temperature", 0.3))
                return Resolved(
                    role=role,
                    spec=spec,
                    degraded=True,
                    reason="敏感任务强制本地模型",
                    chain=(spec,) + tuple(self._spec(r) for r in conf.get("fallback", ())),
                )

        chain = (primary,) + tuple(self._spec(r) for r in conf.get("fallback", ()))
        return Resolved(role=role, spec=primary, chain=chain)

    def all_roles(self) -> list[str]:
        return sorted(self.roles)

    def snapshot(self) -> dict[str, str]:
        """启动时快照，写进 task.model_version，用于"是不是换模型导致的"排查。"""
        return {r: self.roles[r]["model"] for r in self.roles}
