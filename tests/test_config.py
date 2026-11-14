"""配置测试：文件驱动、环境变量覆盖、拼错要炸、路径解析。"""
import shutil
import unittest
from pathlib import Path

from app.agent.tools import load_registry
from app.config import AppConfig, ConfigError

HERE = Path(__file__).parent


class TestLoad(unittest.TestCase):
    def test_defaults_from_yaml(self):
        cfg = AppConfig.load(env={})
        self.assertEqual(cfg.server.port, 8000)
        self.assertTrue(cfg.database.dsn.startswith("postgresql://"))
        self.assertEqual(cfg.runtime.agent_backend, "auto")
        self.assertEqual(cfg.limits.max_steps, 8)
        self.assertTrue(cfg.models_path.exists())
        self.assertTrue(cfg.tools_path.exists())

    def test_env_overrides(self):
        cfg = AppConfig.load(
            env={
                "DATABASE_URL": "postgresql://u:p@db:5432/x",
                "AGENT_BACKEND": "echo",
                "PORT": "9999",
                "HOST": "0.0.0.0",
            }
        )
        self.assertEqual(cfg.database.dsn, "postgresql://u:p@db:5432/x")
        self.assertEqual(cfg.runtime.agent_backend, "echo")
        self.assertEqual(cfg.server.port, 9999)
        self.assertEqual(cfg.server.host, "0.0.0.0")

    def test_prompt_and_tools_resolve_from_config(self):
        cfg = AppConfig.load(env={})
        self.assertIn("个人助理", cfg.read_prompt())
        registry = load_registry(cfg.tools_path)
        self.assertIn("notes.create", registry.names())
        self.assertIn("time.now", registry.names())


class TestBadConfig(unittest.TestCase):
    def _case_dir(self, app_yaml: str) -> Path:
        d = HERE / "_cfg_case"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
        (d / "app.yaml").write_text(app_yaml, encoding="utf-8")
        return d

    def tearDown(self):
        shutil.rmtree(HERE / "_cfg_case", ignore_errors=True)

    def test_missing_file(self):
        empty = HERE / "_cfg_empty"
        shutil.rmtree(empty, ignore_errors=True)
        empty.mkdir()
        try:
            with self.assertRaises(ConfigError):
                AppConfig.load(config_dir=empty, env={})
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_typo_in_field_name_fails_loudly(self):
        # database 拼成 databse：宁可起不来，也别带着错配置静默运行
        d = self._case_dir("databse:\n  dsn: postgresql://a/b\n")
        with self.assertRaises(ConfigError) as cm:
            AppConfig.load(config_dir=d, env={})
        self.assertIn("databse", str(cm.exception))

    def test_missing_required_dsn(self):
        d = self._case_dir("server:\n  port: 8000\n")
        with self.assertRaises(ConfigError):
            AppConfig.load(config_dir=d, env={})

    def test_broken_yaml(self):
        d = self._case_dir("database: [unclosed\n")
        with self.assertRaises(ConfigError):
            AppConfig.load(config_dir=d, env={})


if __name__ == "__main__":
    unittest.main()
