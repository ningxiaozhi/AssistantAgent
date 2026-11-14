"""记忆层测试：页面模型、索引、两段式检索、写入合并。

重点钉住三个行为：
  1. 索引里只有 name + description，**不含正文**（渐进式披露的前提）
  2. 检索先定库再定页，画像库常驻
  3. 写入能合并就合，不新建重复页
"""
import asyncio
import shutil
import unittest
from pathlib import Path

import yaml

from app.config import ROOT, AppConfig
from app.memory.router import MemoryConfig, MemoryConfigError, MemoryRouter, MemoryWriter
from app.memory.store import LocalWikiStore
from app.memory.wiki import WikiPage, build_index, split_frontmatter

HERE = Path(__file__).parent
SEED = ROOT / "examples" / "wiki"
MEMORY_YAML = AppConfig.load(env={}).memory_path


def run(coro):
    return asyncio.run(coro)


class TestPageModel(unittest.TestCase):
    def test_roundtrip(self):
        text = (
            "---\nname: 定价结论\ndescription: 阶梯定价与账期\ntype: work\n"
            "tags: [客户]\nupdated: 2026-10-28\n---\n\n## 结论\n85 折\n"
        )
        page = WikiPage.from_markdown("work", "pricing.md", text)
        self.assertEqual(page.name, "定价结论")
        self.assertEqual(page.description, "阶梯定价与账期")
        self.assertEqual(page.frontmatter.tags, ["客户"])
        self.assertIn("85 折", page.body)
        again = WikiPage.from_markdown("work", "pricing.md", page.to_markdown())
        self.assertEqual(again.name, page.name)
        self.assertEqual(again.body.strip(), page.body.strip())

    def test_missing_frontmatter_falls_back_to_filename(self):
        fm, body = split_frontmatter("没有 frontmatter 的正文")
        self.assertEqual(fm, {})
        self.assertEqual(body, "没有 frontmatter 的正文")

    def test_frontmatter_without_name_uses_path(self):
        page = WikiPage.from_markdown("learn", "ddia-ch4.md", "---\ndescription: d\n---\n正文")
        self.assertIn("ddia", page.name)

    def test_index_has_no_body(self):
        pages = [
            WikiPage.new("work", "a.md", "定价结论", "阶梯定价", "正文里有一个密钥 sk-123"),
        ]
        index = build_index("工作", pages)
        self.assertIn("定价结论", index)
        self.assertIn("阶梯定价", index)
        self.assertNotIn("sk-123", index, "索引不能带正文，否则 token 又爆了")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.root = HERE / "_wiki_case"
        shutil.rmtree(self.root, ignore_errors=True)
        self.store = LocalWikiStore(self.root, seed_dir=SEED)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_seed_copies_examples(self):
        pages = run(self.store.list_pages("work"))
        self.assertTrue(pages)
        self.assertEqual(pages[0].kb, "work")

    def test_put_and_get(self):
        page = WikiPage.new("learn", "new-page.md", "新页面", "一句话描述", "正文")
        run(self.store.put_page(page))
        got = run(self.store.get_page("learn", "new-page.md"))
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "新页面")
        # 索引被自动刷新
        index = run(self.store.index_text("learn", "学习"))
        self.assertIn("新页面", index)

    def test_delete(self):
        page = WikiPage.new("learn", "tmp.md", "临时", "d", "b")
        run(self.store.put_page(page))
        self.assertTrue(run(self.store.delete_page("learn", "tmp.md")))
        self.assertIsNone(run(self.store.get_page("learn", "tmp.md")))

    def test_index_file_not_listed_as_page(self):
        run(self.store.rebuild_index("work", "工作"))
        paths = [p.path for p in run(self.store.list_pages("work"))]
        self.assertFalse(any(p.endswith("INDEX.md") for p in paths))


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.root = HERE / "_wiki_case"
        shutil.rmtree(self.root, ignore_errors=True)
        self.config = MemoryConfig.load(MEMORY_YAML)
        self.store = LocalWikiStore(self.root, seed_dir=SEED)
        self.router = MemoryRouter(self.config, self.store)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_work_query_hits_work_kb(self):
        sel = run(self.router.route("上次和张总聊的定价结论是什么"))
        self.assertIn("work", sel.kbs)
        self.assertTrue(any(h.kb == "work" for h in sel.hits))

    def test_health_query_hits_life(self):
        sel = run(self.router.route("体检报告里尿酸多少"))
        self.assertIn("life", sel.kbs)

    def test_me_is_always_on(self):
        # 画像库常驻：即使 query 完全不相关也要带上
        sel = run(self.router.route("帮我订个会议室"))
        self.assertIn("me", sel.kbs)

    def test_profile_hides_work_from_learning(self):
        sel = run(self.router.route("张总的定价结论", profile="learning"))
        self.assertNotIn("work", sel.kbs)

    def test_max_kbs_respected(self):
        sel = run(self.router.route("张总的定价、体检报告、还有书里的笔记"))
        self.assertLessEqual(len(sel.kbs), self.config.defaults.max_kbs_per_query)

    def test_hits_carry_page_pointer(self):
        sel = run(self.router.route("张总的定价结论"))
        self.assertTrue(sel.hits)
        hit = sel.hits[0]
        self.assertTrue(hit.path.endswith(".md"))
        self.assertTrue(hit.description, "description 为空说明检索会一直不中")

    def test_unknown_profile_raises(self):
        with self.assertRaises(MemoryConfigError):
            run(self.router.route("hi", profile="nope"))


class TestWriter(unittest.TestCase):
    def setUp(self):
        self.root = HERE / "_wiki_case"
        shutil.rmtree(self.root, ignore_errors=True)
        self.config = MemoryConfig.load(MEMORY_YAML)
        self.store = LocalWikiStore(self.root, seed_dir=SEED)
        self.writer = MemoryWriter(self.config, self.store)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_new_topic_creates_page(self):
        res = run(self.writer.write("周末想去杭州看西湖，顺便吃片儿川", kb="life"))
        self.assertEqual(res["action"], "created")
        got = run(self.store.get_page("life", res["path"]))
        self.assertIn("杭州", got.body)

    def test_related_topic_merges_into_existing_page(self):
        res = run(self.writer.write("张总的定价：阶梯定价 85 折，账期 60 天", kb="work"))
        self.assertEqual(res["action"], "merged", "相似内容不该新建第二页")
        self.assertEqual(res["path"], "pricing-2026-10.md")

    def test_same_content_is_not_appended_twice(self):
        text = "张总的定价：阶梯定价 85 折，账期 60 天"
        first = run(self.writer.write(text, kb="work"))
        self.assertEqual(first["action"], "merged")
        second = run(self.writer.write(text, kb="work"))
        self.assertEqual(second["action"], "skipped", "同一段内容不能重复追加")
        page = run(self.store.get_page("work", first["path"]))
        self.assertEqual(page.body.count("## 补充"), 1, "合并只应追加一个小节")

    def test_infer_kb_from_text(self):
        res = run(self.writer.write("体检说尿酸偏高，要注意饮食"))
        self.assertEqual(res["kb"], "life")

    def test_unknown_kb_rejected(self):
        with self.assertRaises(MemoryConfigError):
            run(self.writer.write("随便", kb="not-exist"))


class TestConfigErrors(unittest.TestCase):
    def setUp(self):
        self.dir = HERE / "_mem_cfg"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_unknown_profile_reference(self):
        p = self.dir / "memory.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "libraries": [{"id": "me", "name": "画像"}],
                    "profiles": {"work": ["me", "nope"]},
                    "defaults": {"fallback": ["me"]},
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        with self.assertRaises(MemoryConfigError) as cm:
            MemoryConfig.load(p)
        self.assertIn("nope", str(cm.exception))

    def test_empty_libraries_rejected(self):
        p = self.dir / "memory.yaml"
        p.write_text("libraries: []\n", encoding="utf-8")
        with self.assertRaises(MemoryConfigError):
            MemoryConfig.load(p)


if __name__ == "__main__":
    unittest.main()
