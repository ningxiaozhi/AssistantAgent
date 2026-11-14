"""端到端冒烟：不启动服务、不连数据库、不调模型，把主链路走一遍。

    python scripts/smoke.py

它会打印：健康检查 / 工具路由 / 记忆检索 / 写入合并 / 一次完整对话。
任何一步抛异常就说明骨架坏了 —— CI 里可以直接跑。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import AppConfig  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> int:
    cfg = AppConfig.load()
    cfg.database.dsn = "postgresql://127.0.0.1:1/none"  # 必然连不上 → 内存模式
    cfg.runtime.agent_backend = "echo"
    cfg.server.log_level = "ERROR"

    with TestClient(create_app(cfg)) as client:
        health = client.get("/health").json()
        print(f"① health      : agent={health['agent']} db={health['db']} tasks={health['running_tasks']}")

        tools = client.get("/api/tools").json()
        print(f"② tools       : {tools['stats']['libraries']} 库 / {tools['stats']['tools']} 工具")

        route = client.get("/api/tools/route", params={"q": "把明天的会挪到下午"}).json()
        print(f"③ tool route  : {route['libraries']} → {route['tools']}")

        memory = client.get("/api/memory").json()
        print(f"④ memory      : {[x['id'] for x in memory['libraries']]} stats={memory['stats']}")

        search = client.get("/api/memory/search", params={"q": "上次和张总聊的定价结论"}).json()
        hits = [f"{h['kb']}/{h['path']}" for h in search["hits"]]
        print(f"⑤ memory hits : kbs={search['kbs']} hits={hits}")

        written = client.post(
            "/api/memory/pages",
            json={"text": "张总的定价：阶梯定价 85 折，账期 60 天", "kb": "work"},
        ).json()
        print(f"⑥ memory write: {written['action']} -> {written['kb']}/{written['path']}")

        task_id = client.post("/api/chat", json={"message": "上次和张总聊的定价结论是什么"}).json()[
            "task_id"
        ]
        task = {}
        for _ in range(80):
            task = client.get(f"/api/tasks/{task_id}").json()
            if task["state"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.05)
        print(f"⑦ chat        : state={task['state']} steps={task['steps']}")
        print("   " + (task.get("output") or "").replace("\n", "\n   "))

        # ⑧ HITL：写动作会停下来问，确认后继续
        hitl_id = client.post("/api/chat", json={"message": "记账：38 块打车"}).json()["task_id"]
        parked = {}
        for _ in range(60):
            parked = client.get(f"/api/tasks/{hitl_id}").json()
            if parked["state"] in ("waiting_confirm", "succeeded", "failed", "cancelled"):
                break
            time.sleep(0.05)
        pending = client.get("/api/confirmations").json()
        tool = (parked.get("pending") or {}).get("tool")
        print(f"⑧ hitl        : state={parked['state']} pending={pending['count']} tool={tool}")

        if parked["state"] == "waiting_confirm":
            msg = client.post(f"/api/tasks/{hitl_id}/confirm", json={"approved": True}).json()["message"]
            print(f"   confirm    : {msg}")
            for _ in range(80):
                final = client.get(f"/api/tasks/{hitl_id}").json()
                if final["state"] in ("succeeded", "failed", "cancelled"):
                    break
                time.sleep(0.05)
            print(f"   result     : state={final['state']}")
        else:
            print("   ⚠️ 期望停在 waiting_confirm，实际没有 —— 检查 hitl.demo_in_echo")

    print("\n✅ 冒烟通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
