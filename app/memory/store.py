"""Wiki 存储：两个实现，同一套异步接口。

LocalWikiStore   —— 文件系统，一个库一个目录，打开就是 Markdown，可直接用编辑器改
PostgresWikiStore —— 线上用，落在 app.wiki_page

为什么保留文件系统实现：
  wiki 的价值在于"人可以随时打开改"。落库以后检索方便了，但你会失去"用 Obsidian 打开
  就能改"这个能力。所以骨架默认走文件，Postgres 作为规模化后的替换项（同一接口）。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Protocol

from app.memory.wiki import WikiPage, build_index

INDEX_FILE = "INDEX.md"
log = logging.getLogger(__name__)


class WikiStore(Protocol):
    async def list_pages(self, kb: str) -> list[WikiPage]: ...
    async def get_page(self, kb: str, path: str) -> WikiPage | None: ...
    async def put_page(self, page: WikiPage) -> WikiPage: ...
    async def delete_page(self, kb: str, path: str) -> bool: ...
    async def index_text(self, kb: str, name: str) -> str: ...


class LocalWikiStore:
    """`data/wiki/<kb>/*.md`，每库一个 `INDEX.md` 指针表。"""

    def __init__(self, root: str | Path = "data/wiki", seed_dir: str | Path | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if seed_dir:
            self._seed(Path(seed_dir))

    # ---------- 初始化 ----------
    def _seed(self, seed_dir: Path) -> None:
        """首次启动把示例库拷进来；已存在的文件不覆盖（用户改过就不要再动）。"""
        if not seed_dir.exists():
            return
        for src in seed_dir.rglob("*.md"):
            rel = src.relative_to(seed_dir)
            dst = self.root / rel
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

    def _dir(self, kb: str) -> Path:
        d = self.root / kb
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------- 读 ----------
    async def list_pages(self, kb: str) -> list[WikiPage]:
        d = self._dir(kb)
        pages: list[WikiPage] = []
        for p in sorted(d.rglob("*.md")):
            if p.name == INDEX_FILE:
                continue
            rel = str(p.relative_to(d)).replace("\\", "/")
            try:
                pages.append(WikiPage.from_markdown(kb, rel, p.read_text(encoding="utf-8")))
            except Exception as e:
                # 单个坏文件不该让整库读不出来，但必须留下痕迹——
                # 静默 continue 会让你在"为什么检索不到"上查半天
                log.warning("页面解析失败 %s/%s: %s", kb, rel, e)
                continue
        return pages

    async def get_page(self, kb: str, path: str) -> WikiPage | None:
        p = self._dir(kb) / path
        if not p.exists():
            return None
        return WikiPage.from_markdown(kb, path, p.read_text(encoding="utf-8"))

    async def index_text(self, kb: str, name: str = "") -> str:
        """优先读磁盘上的 INDEX.md（用户可能手工改过）；没有就用页面重建。"""
        p = self._dir(kb) / INDEX_FILE
        if p.exists():
            return p.read_text(encoding="utf-8")
        return build_index(name or kb, await self.list_pages(kb))

    # ---------- 写 ----------
    async def put_page(self, page: WikiPage) -> WikiPage:
        d = self._dir(page.kb)
        target = d / page.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page.to_markdown(), encoding="utf-8")
        await self.rebuild_index(page.kb)
        return page

    async def delete_page(self, kb: str, path: str) -> bool:
        target = self._dir(kb) / path
        if not target.exists():
            return False
        target.unlink()
        await self.rebuild_index(kb)
        return True

    async def rebuild_index(self, kb: str, name: str = "") -> str:
        pages = await self.list_pages(kb)
        text = build_index(name or kb, pages)
        (self._dir(kb) / INDEX_FILE).write_text(text, encoding="utf-8")
        return text

    # ---------- 观测 ----------
    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for d in sorted(self.root.iterdir()):
            if d.is_dir():
                out[d.name] = len([p for p in d.rglob("*.md") if p.name != INDEX_FILE])
        return out


class PostgresWikiStore:
    """⚠️ EXPERIMENTAL：与 LocalWikiStore 同一接口，尚未在真实 pgvector 上验证。

    正文存 app.wiki_page.body；向量索引在 app.kb_chunk（见 vector.py）。
    切过来只需要在 main.py 里换一行构造。
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    async def list_pages(self, kb: str) -> list[WikiPage]:
        rows = await self.db.fetch(
            "SELECT kb_key, path, title, summary, type, body FROM app.wiki_page WHERE kb_key = $1 ORDER BY path",
            kb,
        )
        return [
            WikiPage.new(
                kb=r["kb_key"],
                path=r["path"],
                name=r["title"] or r["path"],
                description=r["summary"] or "",
                body=r["body"] or "",
                page_type=r["type"],
            )
            for r in rows
        ]

    async def get_page(self, kb: str, path: str) -> WikiPage | None:
        row = await self.db.fetchrow(
            "SELECT kb_key, path, title, summary, type, body FROM app.wiki_page WHERE kb_key = $1 AND path = $2",
            kb,
            path,
        )
        if row is None:
            return None
        return WikiPage.new(
            kb=row["kb_key"],
            path=row["path"],
            name=row["title"] or row["path"],
            description=row["summary"] or "",
            body=row["body"] or "",
            page_type=row["type"],
        )

    async def put_page(self, page: WikiPage) -> WikiPage:
        await self.db.execute(
            """
            INSERT INTO app.wiki_page (kb_key, path, title, summary, type, body, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (kb_key, path) DO UPDATE
               SET title = EXCLUDED.title,
                   summary = EXCLUDED.summary,
                   type = EXCLUDED.type,
                   body = EXCLUDED.body,
                   updated_at = now()
            """,
            page.kb,
            page.path,
            page.name,
            page.description,
            page.frontmatter.type,
            page.body,
        )
        return page

    async def delete_page(self, kb: str, path: str) -> bool:
        result = await self.db.execute(
            "DELETE FROM app.wiki_page WHERE kb_key = $1 AND path = $2", kb, path
        )
        return result.endswith("1")

    async def index_text(self, kb: str, name: str = "") -> str:
        return build_index(name or kb, await self.list_pages(kb))
