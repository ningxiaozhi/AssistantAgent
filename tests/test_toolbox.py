"""工具分层治理测试：库划分、路由、profile 过滤、权限封顶。

这一层的测试价值在于：**路由错了能复现**。
所以打分是纯确定性的（触发词 + 文本重合），不调模型。
"""
import shutil
import unittest
from pathlib import Path

import yaml

from app.agent.toolbox import ToolCatalog, ToolConfigError
from app.config import AppConfig

HERE = Path(__file__).parent
TOOLS_YAML = AppConfig.load(env={}).tools_path


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.catalog = ToolCatalog.load(TOOLS_YAML)

    def test_libraries_and_tools_loaded(self):
        self.assertEqual(len(self.catalog.library_ids()), 7)
        self.assertEqual(self.catalog.stats()["tools"], 11)

    def test_every_library_has_tools(self):
        for lib_id in self.catalog.library_ids():
            self.assertGreater(len(self.catalog.tools_of(lib_id)), 0, f"{lib_id} 是空库")

    def test_mail_library_caps_permission(self):
        # 邮件库封顶 L2：发送这种动作根本不该出现在库里
        self.assertEqual(self.catalog.library("mail").max_permission, "L2")

    def test_summary_shape(self):
        row = self.catalog.summary()[0]
        for key in ("id", "name", "tools", "max_permission"):
            self.assertIn(key, row)


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.catalog = ToolCatalog.load(TOOLS_YAML)
        from app.agent.toolbox import ToolRouter

        self.router = ToolRouter(self.catalog)

    def _libs(self, text, **kw):
        return self.router.route(text, **kw).libraries

    def test_calendar_intent(self):
        self.assertIn("calendar", self._libs("把明天的产品评审挪到下午两点"))

    def test_mail_intent(self):
        self.assertEqual(self._libs("给张总回一封邮件")[0], "mail")

    def test_notes_intent(self):
        self.assertIn("notes", self._libs("记一下：周四前把数据发给财务"))

    def test_task_intent(self):
        self.assertIn("task", self._libs("提醒我周五交周报"))

    def test_research_intent(self):
        self.assertIn("research", self._libs("帮我搜一下 DDIA 第 4 章的资料"))

    def test_life_intent(self):
        self.assertIn("life", self._libs("今天打车花了 38 块，记一下"))

    def test_max_two_libraries(self):
        sel = self.router.route("明天提醒我记一下，再搜一下论文并排个日程")
        self.assertLessEqual(len(sel.libraries), 2)

    def test_fallback_when_nothing_matches(self):
        sel = self.router.route("嗯嗯")
        self.assertTrue(sel.libraries)
        self.assertIn("无匹配", sel.reason)
        self.assertTrue(set(sel.libraries) <= set(self.catalog.defaults.fallback))

    def test_profile_limits_visibility(self):
        # 学习教练看不到邮件库
        sel = self.router.route("给张总发邮件", profile="learning")
        self.assertNotIn("mail", sel.libraries)

    def test_unknown_profile_raises(self):
        with self.assertRaises(ToolConfigError):
            self.router.route("hi", profile="no-such-profile")

    def test_explicit_library_wins(self):
        sel = self.router.route("随便说点什么", explicit=["calendar"])
        self.assertEqual(sel.libraries, ["calendar"])
        self.assertEqual(sel.reason, "显式指定")

    def test_explicit_outside_profile_rejected(self):
        with self.assertRaises(ToolConfigError):
            self.router.route("x", profile="learning", explicit=["mail"])


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.catalog = ToolCatalog.load(TOOLS_YAML)

    def test_resolve_returns_only_selected_library_tools(self):
        from app.agent.toolbox import ToolRouter

        sel = ToolRouter(self.catalog).route("把明天的会挪到下午")
        registry = self.catalog.resolve(sel)
        names = registry.names()
        self.assertTrue(names)
        for name in names:
            self.assertTrue(name.startswith(("calendar.", "task.", "system.", "notes.")), name)
        self.assertLess(len(names), 11, "不该把全部工具都挂上")

    def test_resolved_tools_carry_library(self):
        from app.agent.toolbox import ToolRouter

        sel = ToolRouter(self.catalog).route("记一下：周五交周报")
        registry = self.catalog.resolve(sel)
        for tool in registry:
            self.assertTrue(tool.library)
            self.assertIn(tool.permission, ("L0", "L1", "L2", "L3"))


class TestGovernanceViolations(unittest.TestCase):
    """配置写错必须在启动时炸，不能等到运行期才发现。"""

    def setUp(self):
        self.dir = HERE / "_tools_case"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, data: dict) -> Path:
        p = self.dir / "tools.yaml"
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return p

    def _base(self, **lib_overrides):
        lib = {
            "id": "demo",
            "name": "演示",
            "description": "d",
            "triggers": ["演示"],
            "max_permission": "L2",
            "tools": [
                {
                    "name": "demo.echo",
                    "handler": "time_now",
                    "description": "回显",
                    "side_effect": "none",
                    "permission": "L0",
                }
            ],
        }
        lib.update(lib_overrides)
        return {"libraries": [lib], "defaults": {"fallback": ["demo"]}}

    def test_permission_above_library_cap_is_rejected(self):
        # 库默认权限也降到 L1，确保先命中的是"工具越权"这条规则
        data = self._base(max_permission="L1", default_permission="L1")
        data["libraries"][0]["tools"][0]["permission"] = "L2"
        with self.assertRaises(ToolConfigError) as cm:
            ToolCatalog.load(self._write(data))
        self.assertIn("超过库上限", str(cm.exception))

    def test_library_default_above_cap_is_rejected(self):
        data = self._base(max_permission="L1")  # default_permission 仍是 L2
        with self.assertRaises(ToolConfigError) as cm:
            ToolCatalog.load(self._write(data))
        self.assertIn("default_permission", str(cm.exception))

    def test_duplicate_tool_name_is_rejected(self):
        data = self._base()
        data["libraries"].append({**data["libraries"][0], "id": "demo2", "name": "演示2"})
        with self.assertRaises(ToolConfigError) as cm:
            ToolCatalog.load(self._write(data))
        self.assertIn("重名", str(cm.exception))

    def test_unknown_handler_is_rejected(self):
        data = self._base()
        data["libraries"][0]["tools"][0]["handler"] = "no_such_handler"
        with self.assertRaises(ToolConfigError) as cm:
            ToolCatalog.load(self._write(data))
        self.assertIn("未知 handler", str(cm.exception))

    def test_unknown_fallback_is_rejected(self):
        data = self._base()
        data["defaults"]["fallback"] = ["not-exist"]
        with self.assertRaises(ToolConfigError):
            ToolCatalog.load(self._write(data))

    def test_empty_catalog_is_rejected(self):
        p = self.dir / "tools.yaml"
        p.write_text("libraries: []\n", encoding="utf-8")
        with self.assertRaises(ToolConfigError):
            ToolCatalog.load(p)


if __name__ == "__main__":
    unittest.main()
