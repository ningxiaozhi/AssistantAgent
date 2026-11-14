"""工具治理：角色(profile) → 库(library) → 工具(tool)。

一次任务取工具的过程是两段式的：

    用户输入 ──► 先定"该用哪个库"（route）
              ──► 再取该库里的工具（resolve）
              ──► 只有这些工具的 schema 会进 prompt

为什么必须分两段：
  工具超过 20 个之后，全量注入会有三个后果 —— 选择准确率掉、上下文被工具定义吃掉、
  权限失控（一个库出事全体背锅）。分库之后，"看不见的工具"等于"不可能被误调用"，
  这本身就是最便宜的安全措施。

打分刻意做成**纯确定性**的（触发词 + 文本重合），不调模型：
  路由错了要能复现、能写测试。等库数量真的到几十个，再在 `_score` 前面加一层
  embedding 召回（保留同一套接口）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.tools import HANDLERS, Tool, ToolRegistry

PERMISSION_RANK = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


class ToolConfigError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolSpec(_Strict):
    name: str
    handler: str
    description: str = ""
    side_effect: str = "reversible"
    permission: str = "L2"
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)


class ToolLibrary(_Strict):
    id: str
    name: str
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    default_permission: str = "L2"
    max_permission: str = "L3"
    enabled: bool = True
    tools: list[ToolSpec] = Field(default_factory=list)


class CatalogDefaults(_Strict):
    max_libraries_per_task: int = 2
    fallback: list[str] = Field(default_factory=lambda: ["notes", "system"])
    default_permission: str = "L2"
    default_side_effect: str = "reversible"


class ToolSelection(BaseModel):
    libraries: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    reason: str = ""
    profile: str | None = None


# ---------------------------------------------------------------- 目录
class ToolCatalog:
    def __init__(
        self,
        libraries: Iterable[ToolLibrary],
        profiles: dict[str, list[str]],
        defaults: CatalogDefaults,
        path: Path | None = None,
    ) -> None:
        self.libraries = {lib.id: lib for lib in libraries}
        self.profiles = profiles
        self.defaults = defaults
        self.path = path
        self._all_tools: dict[str, ToolSpec] = {}

    # ---------- 载入与校验 ----------
    @classmethod
    def load(cls, path: str | Path) -> "ToolCatalog":
        p = Path(path)
        if not p.exists():
            raise ToolConfigError(f"工具配置不存在: {p}")
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        try:
            libs = [ToolLibrary(**item) for item in raw.get("libraries", [])]
            defaults = CatalogDefaults(**(raw.get("defaults") or {}))
            profiles = {k: list(v) for k, v in (raw.get("profiles") or {}).items()}
        except (ValidationError, TypeError) as e:
            raise ToolConfigError(f"工具配置校验失败 {p}:\n{e}") from e

        catalog = cls(libs, profiles, defaults, p)
        catalog.validate()
        return catalog

    def validate(self) -> None:
        if not self.libraries:
            raise ToolConfigError("至少要定义一个 library")

        seen: dict[str, str] = {}
        for lib in self.libraries.values():
            if PERMISSION_RANK.get(lib.max_permission) is None:
                raise ToolConfigError(f"库 {lib.id}: 非法 max_permission {lib.max_permission}")
            if PERMISSION_RANK.get(lib.default_permission, 99) > PERMISSION_RANK[lib.max_permission]:
                raise ToolConfigError(
                    f"库 {lib.id}: default_permission {lib.default_permission} "
                    f"超过 max_permission {lib.max_permission}"
                )
            for spec in lib.tools:
                if spec.name in seen:
                    raise ToolConfigError(
                        f"工具重名: {spec.name}（{seen[spec.name]} 与 {lib.id}）"
                    )
                seen[spec.name] = lib.id
                if spec.handler not in HANDLERS:
                    raise ToolConfigError(
                        f"库 {lib.id} / 工具 {spec.name}: 未知 handler '{spec.handler}'"
                        f"（可用：{', '.join(sorted(HANDLERS))}）"
                    )
                if PERMISSION_RANK.get(spec.permission) is None:
                    raise ToolConfigError(f"工具 {spec.name}: 非法 permission {spec.permission}")
                # 治理的核心：工具不能突破所属库的权限上限
                if PERMISSION_RANK[spec.permission] > PERMISSION_RANK[lib.max_permission]:
                    raise ToolConfigError(
                        f"库 {lib.id} / 工具 {spec.name}: permission {spec.permission} "
                        f"超过库上限 {lib.max_permission}"
                    )
                self._all_tools[spec.name] = spec

        for name, lib_ids in self.profiles.items():
            unknown = [x for x in lib_ids if x not in self.libraries]
            if unknown:
                raise ToolConfigError(f"profile {name} 引用了不存在的库: {unknown}")
        unknown_fb = [x for x in self.defaults.fallback if x not in self.libraries]
        if unknown_fb:
            raise ToolConfigError(f"defaults.fallback 引用了不存在的库: {unknown_fb}")

    # ---------- 查询 ----------
    def library(self, lib_id: str) -> ToolLibrary | None:
        return self.libraries.get(lib_id)

    def library_ids(self) -> list[str]:
        return sorted(self.libraries)

    def tools_of(self, lib_id: str) -> list[ToolSpec]:
        lib = self.libraries.get(lib_id)
        return [t for t in lib.tools if t.enabled] if lib else []

    def allowed_by_profile(self, profile: str | None) -> list[str]:
        """角色决定可见范围。没指定 profile 就看全部。"""
        if not profile:
            return [i for i, lib in self.libraries.items() if lib.enabled]
        ids = self.profiles.get(profile)
        if ids is None:
            raise ToolConfigError(f"未定义的 profile: {profile}（可用：{', '.join(self.profiles)}）")
        return [i for i in ids if self.libraries[i].enabled]

    # ---------- 装配 ----------
    def resolve(self, selection: ToolSelection) -> ToolRegistry:
        """把选中的库展开成一张扁平工具表（这才是最终进 prompt 的东西）。"""
        registry = ToolRegistry()
        for lib_id in selection.libraries:
            lib = self.libraries.get(lib_id)
            if lib is None:
                continue
            for spec in lib.tools:
                if not spec.enabled:
                    continue
                registry.register(
                    Tool(
                        name=spec.name,
                        description=spec.description,
                        side_effect=spec.side_effect,
                        permission=spec.permission,
                        handler=HANDLERS[spec.handler],
                        library=lib.id,
                    )
                )
        return registry

    # ---------- 观测 ----------
    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "id": lib.id,
                "name": lib.name,
                "description": lib.description,
                "tools": len([t for t in lib.tools if t.enabled]),
                "max_permission": lib.max_permission,
                "enabled": lib.enabled,
            }
            for lib in self.libraries.values()
        ]

    def stats(self) -> dict[str, Any]:
        return {
            "libraries": len(self.libraries),
            "tools": len(self._all_tools),
            "profiles": sorted(self.profiles),
        }


# ---------------------------------------------------------------- 路由
class ToolRouter:
    """先定库，再定工具。纯确定性打分，可复现、可测试。"""

    def __init__(self, catalog: ToolCatalog) -> None:
        self.catalog = catalog

    def route(
        self,
        text: str,
        profile: str | None = None,
        explicit: list[str] | None = None,
    ) -> ToolSelection:
        allowed = self.catalog.allowed_by_profile(profile)
        text_l = (text or "").lower()

        # 1) 显式指定（子代理直连，或用户说"就用日历"）
        if explicit:
            chosen = [x for x in explicit if x in allowed][: self.catalog.defaults.max_libraries_per_task]
            if not chosen:
                raise ToolConfigError(f"显式指定的库都不在 profile {profile} 的可见范围内")
            return self._build(chosen, {}, "显式指定", profile)

        # 2) 打分
        scores = {lib_id: self._score(text_l, lib_id) for lib_id in allowed}
        ranked = [k for k, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v > 0]
        chosen = ranked[: self.catalog.defaults.max_libraries_per_task]

        if chosen:
            reason = "命中库: " + ", ".join(f"{c}({scores[c]:.1f})" for c in chosen)
        else:
            # 3) 兜底：不能让模型"手上没有工具"
            fb = [x for x in self.catalog.defaults.fallback if x in allowed]
            chosen = fb or allowed[:1]
            reason = "无匹配，回落到兜底库"

        return self._build(chosen, scores, reason, profile)

    # ---------- 内部 ----------
    def _build(
        self, lib_ids: list[str], scores: dict[str, float], reason: str, profile: str | None
    ) -> ToolSelection:
        tools: list[str] = []
        for lib_id in lib_ids:
            tools.extend(t.name for t in self.catalog.tools_of(lib_id))
        return ToolSelection(
            libraries=lib_ids,
            tools=tools,
            scores={k: round(v, 2) for k, v in scores.items() if v > 0},
            reason=reason,
            profile=profile,
        )

    def _score(self, text: str, lib_id: str) -> float:
        if not text:
            return 0.0
        lib = self.catalog.libraries[lib_id]
        score = 0.0

        # 触发词命中权重最高：它是人写的，最准
        for trigger in lib.triggers:
            if trigger and trigger in text:
                score += 3.0

        score += 2.0 * _overlap(text, lib.description)
        for spec in lib.tools:
            short = spec.name.split(".")[-1].lower()
            if short and short in text:
                score += 2.0
            score += _overlap(text, spec.description)
        return score


def _overlap(query: str, text: str) -> float:
    """字符二元组重合度：中文没空格，这个够用且不引入依赖。"""
    def grams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s or "")
        return {s[i : i + 2] for i in range(len(s) - 1)}

    qg, tg = grams(query), grams(text)
    if not qg or not tg:
        return 0.0
    return len(qg & tg) / len(qg)
