"""记忆层：LLM Wiki 多知识库。

    wiki.py    页面模型（frontmatter / 索引 / 渐进式披露）
    store.py   存储（文件系统 / Postgres），同一套异步接口
    router.py  检索路由（先定库，再定页）+ 写入合并策略
    vector.py  pgvector 索引（页面之上的一层，不是替代）
"""

from app.memory.router import MemoryConfig, MemoryRouter, MemorySelection, MemoryWriter
from app.memory.store import LocalWikiStore, PostgresWikiStore, WikiStore
from app.memory.wiki import WikiPage, build_index

__all__ = [
    "MemoryConfig",
    "MemoryRouter",
    "MemorySelection",
    "MemoryWriter",
    "LocalWikiStore",
    "PostgresWikiStore",
    "WikiStore",
    "WikiPage",
    "build_index",
]
