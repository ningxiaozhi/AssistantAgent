"""模型路由测试：角色分工、敏感强制本地、降级链、配置校验。"""
import unittest

from app.agent.routing import ConfigError, ModelRouter


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.router = ModelRouter("config/models.yaml", force_local_on_sensitive=True)

    def test_intent_uses_fast_local_model(self):
        r = self.router.resolve("intent")
        self.assertEqual(r.spec.provider, "local")
        self.assertFalse(r.degraded)

    def test_planner_uses_strong_model(self):
        r = self.router.resolve("planner")
        self.assertEqual(r.spec.provider, "ds")
        self.assertEqual(r.spec.ref, "ds/deepseek-chat")

    def test_planner_has_fallback_chain(self):
        r = self.router.resolve("planner")
        refs = [s.ref for s in r.chain]
        self.assertIn("qwen/qwen-max", refs)

    def test_sensitive_forces_local(self):
        r = self.router.resolve("subagent.work", sensitive=True)
        self.assertEqual(r.spec.provider, "local")
        self.assertTrue(r.degraded)
        self.assertIn("敏感", r.reason)

    def test_sensitive_local_role_stays_undegraded(self):
        r = self.router.resolve("subagent.life", sensitive=True)
        self.assertEqual(r.spec.provider, "local")
        self.assertFalse(r.degraded, "本来就是本地模型，不算降级")

    def test_unknown_role_raises(self):
        with self.assertRaises(ConfigError):
            self.router.resolve("no-such-role")

    def test_snapshot_is_stable(self):
        snap = self.router.snapshot()
        self.assertIn("intent", snap)
        self.assertIn("planner", snap)


class TestBadConfig(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            ModelRouter("config/does-not-exist.yaml")

    def test_ref_without_provider(self):
        from pathlib import Path

        # 不用 tempfile：受限环境下它对临时目录做 chmod 会被拒绝
        p = Path(__file__).parent / "_bad_models.yaml"
        try:
            p.write_text("providers: {}\nroles:\n  x:\n    model: qwen-max\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                ModelRouter(p)
        finally:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
