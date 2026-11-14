"""接口测试：不依赖数据库、不依赖模型，验证整条 HTTP 链路。"""
import time
import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client() -> TestClient:
    settings = Settings(
        database_url="postgresql://127.0.0.1:1/none",  # 必然连不上 → 内存模式
        agent_backend="echo",
        log_level="WARNING",
    )
    return TestClient(create_app(settings))


class TestApi(unittest.TestCase):
    def test_health_degrades_gracefully(self):
        with _client() as client:
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["agent"], "echo")
            self.assertIn("down", body["db"])

    def test_chat_completes(self):
        with _client() as client:
            r = client.post("/api/chat", json={"message": "记一下：周四前发数据"})
            self.assertEqual(r.status_code, 202)
            task_id = r.json()["task_id"]

            task = self._wait(client, task_id, {"succeeded", "failed"})
            self.assertEqual(task["state"], "succeeded", task.get("error"))
            self.assertIn("echo", task["output"])

    def test_cancel_returns_409_when_finished(self):
        with _client() as client:
            r = client.post("/api/chat", json={"message": "短任务"})
            task_id = r.json()["task_id"]
            self._wait(client, task_id, {"succeeded", "failed"})
            r2 = client.post(f"/api/tasks/{task_id}/cancel", json={"reason": "太晚了"})
            self.assertEqual(r2.status_code, 409)

    def test_cancel_unknown_returns_404(self):
        with _client() as client:
            self.assertEqual(client.post("/api/tasks/nope/cancel").status_code, 404)
            self.assertEqual(client.get("/api/tasks/nope").status_code, 404)

    def test_trace_endpoint_works_without_db(self):
        with _client() as client:
            r = client.post("/api/chat", json={"message": "hi"})
            task_id = r.json()["task_id"]
            self._wait(client, task_id, {"succeeded", "failed"})
            r2 = client.get(f"/api/tasks/{task_id}/trace")
            self.assertEqual(r2.status_code, 200)
            self.assertIn("events", r2.json())

    @staticmethod
    def _wait(client: TestClient, task_id: str, states: set[str], timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        task = {}
        while time.time() < deadline:
            task = client.get(f"/api/tasks/{task_id}").json()
            if task.get("state") in states:
                return task
            time.sleep(0.03)
        return task


if __name__ == "__main__":
    unittest.main()
