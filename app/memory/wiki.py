"""LLM Wiki 的页面模型：frontmatter + 正文 + 索引。

一页长这样：

    ---
    name: DDIA 第 4 章 编码与演化
    description: 数据编码格式的兼容性取舍，回答"为什么不用 JSON 存事件"时用
    type: learn
    tags: [分布式, 序列化]
    updated: 2026-11-12
    ---

    ## 结论
    ...

两个关键约定：

1. **description 是给检索用的，不是给人看的**
   写成"关于 XX"等于没写，检索永远不命中。索引里只放 name + description，
   所以这两行决定了这条知识会不会被用上。

2. **索引只放指针，不放正文**
   一次注入 50 条索引 ≈ 1.5k token；一次注入 50 页正文 ≈ 25 万 token。
   这就是"渐进式披露"：先给目录，需要再翻页。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

FRONTMATTER_DELIM = "---"


class Frontmatter(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    type: str | None = None
    tags: list[str] = Field(default_factory=list)
    updated: str | None = None
    sources: list[str] = Field(default_factory=list)

    @field_validator("updated", mode="before")
    @classmethod
    def _coerce_date(cls, value: Any) -> Any:
        # YAML 会把 `updated: 2026-10-28` 解析成 date 对象，统一转成 ISO 字符串
        if isinstance(value, (date, datetime)):
            return value.isoformat()[:10]
        return value

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(exclude_none=True).items() if v != []}


@dataclass
class WikiPage:
    kb: str
    path: str  # 相对库根，如 "ddia-ch4.md"
    frontmatter: Frontmatter
    body: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------- 检索用 ----------
    @property
    def name(self) -> str:
        return self.frontmatter.name

    @property
    def description(self) -> str:
        return self.frontmatter.description

    def searchable_text(self) -> str:
        """打分时看的全部内容。name/description 权重靠前，因为它们是人为提炼的。"""
        parts = [self.name, self.description, " ".join(self.frontmatter.tags), self.body]
        return "\n".join(p for p in parts if p)

    # ---------- 序列化 ----------
    def to_markdown(self) -> str:
        fm = yaml.safe_dump(
            self.frontmatter.to_dict(), allow_unicode=True, sort_keys=False
        ).strip()
        body = self.body.strip("\n")
        return f"{FRONTMATTER_DELIM}\n{fm}\n{FRONTMATTER_DELIM}\n\n{body}\n"

    @classmethod
    def from_markdown(cls, kb: str, path: str, text: str) -> "WikiPage":
        fm_dict, body = split_frontmatter(text)
        if "name" not in fm_dict:
            fm_dict["name"] = path.rsplit(".", 1)[0].replace("-", " ")
        return cls(kb=kb, path=path, frontmatter=Frontmatter(**fm_dict), body=body)

    @classmethod
    def new(
        cls,
        kb: str,
        path: str,
        name: str,
        description: str = "",
        body: str = "",
        tags: list[str] | None = None,
        page_type: str | None = None,
        sources: list[str] | None = None,
    ) -> "WikiPage":
        return cls(
            kb=kb,
            path=path,
            frontmatter=Frontmatter(
                name=name,
                description=description,
                type=page_type or kb,
                tags=tags or [],
                updated=_today(),
                sources=sources or [],
            ),
            body=body,
        )


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆 frontmatter 与正文。没有 frontmatter 的文件也允许（返回空字典）。"""
    stripped = (text or "").lstrip("\ufeff").lstrip()
    if not stripped.startswith(FRONTMATTER_DELIM):
        return {}, text

    lines = stripped.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            end = i
            break
    if end is None:
        return {}, text

    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, "\n".join(lines[end + 1 :]).strip("\n")


def build_index(kb_name: str, pages: list[WikiPage]) -> str:
    """一个库的 INDEX.md：只有指针，没有正文。"""
    lines = [f"# {kb_name}", ""]
    for p in sorted(pages, key=lambda x: x.path):
        desc = p.description.strip() or "（缺 description，检索不会命中）"
        lines.append(f"- [{p.name}]({p.path}) — {desc}")
    if len(lines) == 2:
        lines.append("_（空库）_")
    return "\n".join(lines) + "\n"


def slugify(text: str, fallback: str | None = None) -> str:
    """文件名：保留中英文数字，其余压成连字符。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (text or "").strip().lower()).strip("-")
    return (s or fallback or f"page-{_today()}")[:60]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
