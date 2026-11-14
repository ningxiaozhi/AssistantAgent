"""Case schema & loader for the personal-assistant eval harness.

一条 case = 一次可复现的任务 + 一组可判定的期望。
关键设计：env 里冻结 now / calendar / memory / tools，否则个人助理的 case 不可复现。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

BASE = Path(__file__).resolve().parent

Status = Literal["succeeded", "failed", "cancelled", "needs_confirm", "interrupted"]


class EnvSpec(BaseModel):
    """环境快照：跑这条 case 时世界长什么样。"""

    now: datetime  # 冻结时间。不冻结，"下周三"在两次跑批里指的就不是同一天
    calendar: str | None = None  # fixtures/calendar_*.yaml
    memory: str | None = None  # fixtures/memory_*.yaml
    wiki: str | None = None  # fixtures/wiki 目录（相对 BASE）
    tools: str = "fixtures/tools.json"

    def resolve(self, value: str | None) -> Path | None:
        if not value:
            return None
        p = Path(value)
        return p if p.is_absolute() else BASE / p


class ArgCheck(BaseModel):
    """对某个工具调用的入参做精确断言（能用程序断言的，绝不交给 judge）。"""

    tool: str
    path: str  # 点号路径，如 "start" 或 "attendees.0"
    equals: Any | None = None
    contains: str | None = None
    exists: bool | None = None


class Expect(BaseModel):
    status: Status
    must_call: list[str] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)
    args: list[ArgCheck] = Field(default_factory=list)
    output_contains: list[str] = Field(default_factory=list)  # 全部都要出现
    output_any: list[str] = Field(default_factory=list)  # 出现任意一个即可
    environment_unchanged: list[str] = Field(default_factory=list)  # ["calendar"] / ["memory"]
    max_steps: int = 8
    max_cost_usd: float = 0.15


class Case(BaseModel):
    id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    weight: float = 1.0
    repeats: int = 1  # 同一 case 跑多次看稳定性（非确定性模型）
    env: EnvSpec
    input: str
    expect: Expect
    must_not: list[str] = Field(default_factory=list)  # 人类可读的负面断言，进报告

    @model_validator(mode="after")
    def _check_consistency(self) -> "Case":
        overlap = set(self.expect.must_call) & set(self.expect.must_not_call)
        if overlap:
            raise ValueError(f"{self.id}: 同一工具同时出现在 must_call 和 must_not_call: {overlap}")
        if self.expect.status in ("cancelled", "needs_confirm") and not self.must_not:
            # 不是硬错误，但提醒：这类 case 通常在防"它悄悄做了"
            pass
        return self


def load_cases(path: str | Path) -> list[Case]:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: 顶层必须是 case 列表")

    cases: list[Case] = []
    seen: set[str] = set()
    for item in raw:
        case = Case.model_validate(item)
        if case.id in seen:
            raise ValueError(f"重复的 case id: {case.id}")
        seen.add(case.id)
        cases.append(case)
    return cases


def filter_cases(cases: list[Case], tags: list[str] | None = None) -> list[Case]:
    if not tags:
        return cases
    wanted = set(tags)
    return [c for c in cases if wanted & set(c.tags)]
