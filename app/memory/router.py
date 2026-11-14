"""记忆检索与写入：先定库，再定页面。

和工具治理同构，但多了一层"渐进式披露"：

    问题 ──► ① 选库（me / work / learn / life / ref）
         ──► ② 库内选页（只看 name + description，不读正文）
         ──► ③ 需要时再取正文

为什么不直接向量 Top-K：
  向量能"找到"，但找到的往往是碎片。页面是能被 LLM 改写、合并、引用的单元，
  所以先按类型分库（这一步就把 80% 的噪音挡掉了），再在库内做检索。
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.text import bigram_overlap, contains_any
from app.memory.store import WikiStore
from app.memory.wiki import WikiPage, slugify


class MemoryConfigError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryDefaults(_Strict):
    max_kbs_per_query: int = 2
    pages_per_kb: int = 3
    min_page_score: float = 0.12
    fallback: list[str] = Field(default_factory=lambda: ["me", "ref"])
    merge_threshold: float = 0.35
    seed_dir: str = "examples/wiki"


class MemoryLibrary(_Strict):
    id: str
    name: str
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    always_on: bool = False
    write_policy: str = "auto"  # auto | confirm | append


class MemoryConfig:
    def __init__(
        self,
        libraries: dict[str, MemoryLibrary],
        profiles: dict[str, list[str]],
        defaults: MemoryDefaults,
        path: Path | None = None,
    ) -> None:
        self.libraries = libraries
        self.profiles = profiles
        self.defaults = defaults
        self.path = path

    @classmethod
    def load(cls, path: str | Path) -> "MemoryConfig":
        p = Path(path)
        if not p.exists():
            raise MemoryConfigError(f"记忆配置不存在: {p}")
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        try:
            libs = {item["id"]: MemoryLibrary(**item) for item in raw.get("libraries", [])}
            defaults = MemoryDefaults(**(raw.get("defaults") or {}))
            profiles = {k: list(v) for k, v in (raw.get("profiles") or {}).items()}
        except (ValidationError, KeyError, TypeError) as e:
            raise MemoryConfigError(f"记忆配置校验失败 {p}:\n{e}") from e

        if not libs:
            raise MemoryConfigError("至少要定义一个知识库")
        for name, ids in profiles.items():
            unknown = [x for x in ids if x not in libs]
            if unknown:
                raise MemoryConfigError(f"profile {name} 引用了不存在的库: {unknown}")
        unknown_fb = [x for x in defaults.fallback if x not in libs]
        if unknown_fb:
            raise MemoryConfigError(f"defaults.fallback 引用了不存在的库: {unknown_fb}")

        return cls(libs, profiles, defaults, p)

    def allowed(self, profile: str | None) -> list[str]:
        if not profile:
            return list(self.libraries)
        ids = self.profiles.get(profile)
        if ids is None:
            raise MemoryConfigError(
                f"未定义的 profile: {profile}（可用：{', '.join(self.profiles)}）"
            )
        return list(ids)

    def summary(self) -> list[dict]:
        return [
            {
                "id": lib.id,
                "name": lib.name,
                "always_on": lib.always_on,
                "write_policy": lib.write_policy,
                "triggers": lib.triggers,
            }
            for lib in self.libraries.values()
        ]


# ---------------------------------------------------------------- 结果模型
class MemoryHit(BaseModel):
    kb: str
    path: str
    name: str
    description: str = ""
    score: float = 0.0


class MemorySelection(BaseModel):
    kbs: list[str] = Field(default_factory=list)
    hits: list[MemoryHit] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    reason: str = ""
    profile: str | None = None


# ---------------------------------------------------------------- 检索
class MemoryRouter:
    def __init__(self, config: MemoryConfig, store: WikiStore) -> None:
        self.config = config
        self.store = store

    async def route(
        self, query: str, profile: str | None = None, explicit: list[str] | None = None
    ) -> MemorySelection:
        allowed = self.config.allowed(profile)
        if explicit:
            chosen = [x for x in explicit if x in allowed][: self.config.defaults.max_kbs_per_query]
            if not chosen:
                raise MemoryConfigError(f"显式指定的库都不在 profile {profile} 的可见范围内")
            return await self._collect(query, chosen, {}, "显式指定", profile)

        # 常驻库优先：画像这类知识只有几行，永远带上比"检索到才给"更省事
        always = [i for i in allowed if self.config.libraries[i].always_on]
        scored = {i: self._score_kb(query, i) for i in allowed if i not in always}
        ranked = [k for k, v in sorted(scored.items(), key=lambda kv: -kv[1]) if v > 0]

        budget = self.config.defaults.max_kbs_per_query
        chosen = always[:budget]
        for kb in ranked:
            if len(chosen) >= budget:
                break
            chosen.append(kb)

        if not chosen:
            chosen = [x for x in self.config.defaults.fallback if x in allowed][:budget]
            reason = "无匹配，回落到兜底库"
        elif ranked:
            reason = "命中库: " + ", ".join(f"{c}({scored.get(c, 0):.1f})" for c in chosen)
        else:
            reason = "仅常驻库"

        return await self._collect(query, chosen, scored, reason, profile)

    def _score_kb(self, query: str, kb_id: str) -> float:
        lib = self.config.libraries[kb_id]
        return 3.0 * contains_any(query, lib.triggers) + 2.0 * bigram_overlap(
            query, lib.description
        )

    async def _collect(
        self,
        query: str,
        kb_ids: list[str],
        scores: dict[str, float],
        reason: str,
        profile: str | None,
    ) -> MemorySelection:
        hits: list[MemoryHit] = []
        for kb_id in kb_ids:
            pages = await self.store.list_pages(kb_id)
            scored = [(self._score_page_text(query, p), p) for p in pages]
            scored.sort(key=lambda x: -x[0])
            keep = 0
            for score, page in scored:
                if keep >= self.config.defaults.pages_per_kb:
                    break
                always = self.config.libraries[kb_id].always_on
                if score < self.config.defaults.min_page_score and not (always and keep == 0):
                    continue
                hits.append(
                    MemoryHit(
                        kb=kb_id,
                        path=page.path,
                        name=page.name,
                        description=page.description,
                        score=round(score, 3),
                    )
                )
                keep += 1
        return MemorySelection(
            kbs=kb_ids,
            hits=hits,
            scores={k: round(v, 2) for k, v in scores.items() if v > 0},
            reason=reason,
            profile=profile,
        )

    @staticmethod
    def _score_page_text(query: str, page: WikiPage) -> float:
        return (
            2.0 * bigram_overlap(query, page.name)
            + 1.5 * bigram_overlap(query, page.description)
            + 1.0 * bigram_overlap(query, page.body)
        )

    async def index_for(self, kb_id: str) -> str:
        lib = self.config.libraries[kb_id]
        return await self.store.index_text(kb_id, lib.name)


# ---------------------------------------------------------------- 写入
class MemoryWriter:
    """写入策略：**能并进已有页面就绝不新建**。

    只增不改的 wiki，半年后同一件事会有五个版本，检索到最老的那个，答得还很自信。
    这里做的是"粗合并"（定位到页 + 追加小节）；真正的语义合并要交给 LLM，
    那是下一步（见 docs/architecture.md §4.1）。
    """

    def __init__(self, config: MemoryConfig, store: WikiStore) -> None:
        self.config = config
        self.store = store

    async def write(
        self,
        text: str,
        kb: str | None = None,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> dict:
        kb_id = kb or self._infer_kb(text)
        if kb_id not in self.config.libraries:
            raise MemoryConfigError(f"未知的库: {kb_id}")

        pages = await self.store.list_pages(kb_id)
        best, best_score = None, 0.0
        for page in pages:
            # 整页参与比对：只看标题会漏掉"内容相关但标题不同"的情况
            haystack = f"{page.name}\n{page.description}\n{page.body}"
            score = bigram_overlap(text, haystack)
            if score > best_score:
                best, best_score = page, score

        need_new = best is None or best_score < self.config.defaults.merge_threshold

        # 幂等：同一段内容已经在页里了就不再追加。
        # 没有这一条，同一件事被记住三次就会出现三个小节。
        if best is not None and text.strip() and text.strip() in best.body:
            return {"action": "skipped", "kb": kb_id, "path": best.path, "score": round(best_score, 3)}

        if need_new:
            heading = title or text.strip().splitlines()[0][:30]
            path = f"{slugify(heading)}.md"
            existing = {p.path for p in pages}
            if path in existing:  # 撞名就加后缀，别覆盖
                path = path.replace(".md", f"-{len(existing) + 1}.md")
            page = WikiPage.new(
                kb=kb_id,
                path=path,
                name=heading,
                description=description or text.strip()[:80],
                body=text.strip(),
                tags=tags,
                sources=[source] if source else None,
            )
            await self.store.put_page(page)
            return {"action": "created", "kb": kb_id, "path": path, "score": 0.0}

        section = f"## {title or '补充'}\n\n{text.strip()}"
        best.body = (best.body.rstrip() + "\n\n" + section).strip()
        best.frontmatter.updated = _today()
        if source:
            best.frontmatter.sources = list({*best.frontmatter.sources, source})
        await self.store.put_page(best)
        return {
            "action": "merged",
            "kb": kb_id,
            "path": best.path,
            "score": round(best_score, 3),
        }

    def _infer_kb(self, text: str) -> str:
        scored = {kb: self._score_kb(text, kb) for kb in self.config.libraries}
        best = max(scored.items(), key=lambda kv: kv[1])
        return best[0] if best[1] > 0 else self.config.defaults.fallback[0]

    def _score_kb(self, query: str, kb_id: str) -> float:
        lib = self.config.libraries[kb_id]
        return 3.0 * contains_any(query, lib.triggers) + 2.0 * bigram_overlap(
            query, lib.description
        )


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
