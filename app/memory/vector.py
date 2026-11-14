"""向量索引：页面之上的一层，不是页面的替代。

位置关系要说清楚：
    wiki 页面（人可读、LLM 可改）   ← 权威内容
          └─ 切块 + 向量           ← 只是索引，坏了可以从页面重建

所以任何时候，"删掉向量重算"都是安全的；反过来不成立。

⚠️ HashEmbedder 是**占位实现**：它按字符哈希出向量，只保证"同文本同向量"，
   不具备任何语义能力。它的用途是让整条链路（切块 → 入库 → 检索 → 取回页面）
   在没有 embedding 模型的情况下也能跑通、也能写测试。
   接真模型时把 `embedder` 换成 bge-m3 即可，其余代码不动。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Protocol, Sequence


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashEmbedder:
    """占位 embedder：确定性、零依赖、无语义。"""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokens(text):
            h = int(hashlib.md5(token.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0 if (h >> 8) % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _tokens(text: str) -> list[str]:
    s = "".join((text or "").split()).lower()
    return [s[i : i + 2] for i in range(max(0, len(s) - 1))]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def chunk_text(text: str, size: int = 240, overlap: int = 40) -> list[str]:
    """按字符切块。真实场景应按 Markdown 标题切，这里保留最小实现。"""
    t = (text or "").strip()
    if len(t) <= size:
        return [t] if t else []
    step = max(1, size - overlap)
    return [t[i : i + size] for i in range(0, len(t), step) if t[i : i + size].strip()]


@dataclass
class VectorHit:
    kb: str
    page_path: str
    seq: int
    content: str
    score: float


class InMemoryVectorStore:
    """测试与小数据量用。真上量换 PgVectorStore。"""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder or HashEmbedder()
        self._rows: list[dict[str, Any]] = []

    async def index_page(self, kb: str, page_path: str, text: str) -> int:
        await self.delete_page(kb, page_path)
        chunks = chunk_text(text)
        if not chunks:
            return 0
        vectors = await self.embedder.embed(chunks)
        for seq, (content, vec) in enumerate(zip(chunks, vectors)):
            self._rows.append(
                {"kb": kb, "page_path": page_path, "seq": seq, "content": content, "vec": vec}
            )
        return len(chunks)

    async def search(self, kb: str, query: str, top_k: int = 5) -> list[VectorHit]:
        qv = (await self.embedder.embed([query]))[0]
        hits = [
            VectorHit(r["kb"], r["page_path"], r["seq"], r["content"], cosine(qv, r["vec"]))
            for r in self._rows
            if r["kb"] == kb
        ]
        hits.sort(key=lambda h: -h.score)
        return hits[:top_k]

    async def delete_page(self, kb: str, page_path: str) -> int:
        before = len(self._rows)
        self._rows = [r for r in self._rows if not (r["kb"] == kb and r["page_path"] == page_path)]
        return before - len(self._rows)

    def count(self) -> int:
        return len(self._rows)


class PgVectorStore:
    """⚠️ EXPERIMENTAL：SQL 已按 pgvector 0.7+ 写，但未在真实库上验证。

    接 AgentScope 时：继承 `VectorStoreBase` 并保持 insert / search / delete / list
    四个方法签名不变，就能直接挂进 `KnowledgeBase` handle。
    """

    def __init__(self, db: Any, embedder: Embedder | None = None) -> None:
        self.db = db
        self.embedder = embedder or HashEmbedder()

    async def index_page(self, kb: str, page_path: str, text: str) -> int:
        await self.delete_page(kb, page_path)
        chunks = chunk_text(text)
        if not chunks:
            return 0
        vectors = await self.embedder.embed(chunks)
        for seq, (content, vec) in enumerate(zip(chunks, vectors)):
            await self.db.execute(
                """
                INSERT INTO app.kb_chunk (kb_key, page_path, seq, content, embedding)
                VALUES ($1, $2, $3, $4, $5::halfvec)
                """,
                kb,
                page_path,
                seq,
                content,
                _vector_literal(vec),
            )
        return len(chunks)

    async def search(self, kb: str, query: str, top_k: int = 5) -> list[VectorHit]:
        qv = (await self.embedder.embed([query]))[0]
        rows = await self.db.fetch(
            """
            SELECT kb_key, page_path, seq, content,
                   1 - (embedding <=> $2::halfvec) AS score
              FROM app.kb_chunk
             WHERE kb_key = $1
             ORDER BY embedding <=> $2::halfvec
             LIMIT $3
            """,
            kb,
            _vector_literal(qv),
            top_k,
        )
        return [
            VectorHit(r["kb_key"], r["page_path"], r["seq"], r["content"], float(r["score"]))
            for r in rows
        ]

    async def delete_page(self, kb: str, page_path: str) -> int:
        result = await self.db.execute(
            "DELETE FROM app.kb_chunk WHERE kb_key = $1 AND page_path = $2", kb, page_path
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0


def _vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
