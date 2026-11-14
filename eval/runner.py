"""跑批器：把 case 喂给被测 Agent，产出可回放的 trace。

三个 adapter：
  null        —— 什么都不做（基线，用来验证计分卡会把烂成绩打出来）
  scripted    —— 按 fixtures/scripts/<case_id>.yaml 演一遍（验证 harness 本身）
  agentscope  —— 接真实 Agent，只需要实现 run() 一个方法

用法：
  python eval/runner.py --agent scripted
  python eval/runner.py --agent null --tag safety
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from schema import BASE, Case, filter_cases, load_cases
from world import MockWorld

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class RunResult:
    case_id: str
    status: str = "failed"
    output: str = ""
    steps: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    effects: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    latency_ms: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    synthetic: bool = True  # 这些数字是脚本编的，接真 Agent 后置 False

    @property
    def called_tools(self) -> list[str]:
        return [c["name"] for c in self.calls]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "status": self.status,
            "output": self.output,
            "steps": self.steps,
            "calls": self.calls,
            "effects": self.effects,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "events": self.events,
            "error": self.error,
            "synthetic": self.synthetic,
        }


class Adapter(Protocol):
    name: str

    def run(self, case: Case, world: MockWorld) -> RunResult: ...


class NullAgent:
    """基线：只会说"好的"，什么都不做。"""

    name = "null"

    def run(self, case: Case, world: MockWorld) -> RunResult:
        return RunResult(case_id=case.id, status="succeeded", output="好的，我来处理。", steps=1, latency_ms=400)


class ScriptedAgent:
    """按剧本执行，用来验证 harness 与计分卡本身是对的。"""

    name = "scripted"

    def run(self, case: Case, world: MockWorld) -> RunResult:
        script_path = BASE / "fixtures" / "scripts.yaml"
        scripts = yaml.safe_load(script_path.read_text(encoding="utf-8")) or {}
        if case.id not in scripts:
            return RunResult(
                case_id=case.id,
                status="failed",
                output="",
                error=f"scripts.yaml 里没有 {case.id} 的剧本",
            )
        script = scripts[case.id]
        says: list[str] = []
        events: list[dict[str, Any]] = []
        cancelled = False

        for step in script.get("steps", []):
            if step.get("cancel"):
                world.request_cancel()
                cancelled = True
                events.append({"type": "cancel", "by": "user"})
                continue

            if step.get("tool"):
                call = world.call(step["tool"], step.get("args") or {})
                events.append({"type": "tool", **call.to_dict()})

            if step.get("say"):
                says.append(step["say"])
                events.append({"type": "say", "text": step["say"]})

        status = script.get("status") or ("cancelled" if cancelled else "succeeded")
        return RunResult(
            case_id=case.id,
            status=status,
            output="\n".join(says),
            steps=len(events),
            calls=[c.to_dict() for c in world.calls],
            effects=list(world.effects),
            cost_usd=float(script.get("cost_usd", 0.02)),
            latency_ms=int(script.get("latency_ms", 1200)),
            events=events,
        )


class AgentScopeAgent:
    """接真实 Agent 的位置。实现下面 6 行即可。"""

    name = "agentscope"

    def run(self, case: Case, world: MockWorld) -> RunResult:
        raise NotImplementedError(
            "接真实 Agent：\n"
            "  1. world 里的 tools 注册成 AgentScope 的 Toolkit（mock 掉真实 API）\n"
            "  2. 把 case.env.now 注入 agent 的系统提示与工具上下文（冻结时间）\n"
            "  3. result = agent.reply(msg)；用 reply_stream 收 AgentEvent 当 events\n"
            "  4. 工具调用从 world.calls 取（因为它走的就是 mock 工具）\n"
            "  5. status 映射：正常结束=succeeded / 挂在 ASK=needs_confirm / 中途取消=cancelled\n"
            "  6. 把真实 cost_usd、latency_ms 填回来，并把 synthetic 置 False"
        )


ADAPTERS: dict[str, type] = {
    "null": NullAgent,
    "scripted": ScriptedAgent,
    "agentscope": AgentScopeAgent,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="个人助理 Agent 评测跑批")
    ap.add_argument("--cases", default=str(BASE / "cases" / "cases.yaml"))
    ap.add_argument("--agent", default="scripted", choices=sorted(ADAPTERS))
    ap.add_argument("--tag", nargs="*", default=None, help="只跑这些 tag")
    ap.add_argument("--out", default=None, help="输出目录")
    args = ap.parse_args(argv)

    cases = filter_cases(load_cases(args.cases), args.tag)
    if not cases:
        print("没有匹配的 case")
        return 2

    adapter = ADAPTERS[args.agent]()
    out_dir = Path(args.out) if args.out else BASE / "report" / f"run-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    records = []
    print(f"\n▶ agent={args.agent}  cases={len(cases)}  out={out_dir}\n")
    for case in cases:
        runs: list[RunResult] = []
        for _ in range(case.repeats):
            world = MockWorld.build(case)
            before = world.snapshot()
            t0 = time.perf_counter()
            try:
                res = adapter.run(case, world)
            except NotImplementedError as e:
                print(f"\n{e}\n")
                return 3
            except Exception as e:  # 适配器崩了也要留下证据
                res = RunResult(case_id=case.id, status="failed", error=f"{type(e).__name__}: {e}")
            res.latency_ms = res.latency_ms or int((time.perf_counter() - t0) * 1000)
            res.effects = res.effects or list(world.effects)
            res.calls = res.calls or [c.to_dict() for c in world.calls]
            runs.append(res)

        records.append(
            {
                "case": case.model_dump(mode="json"),
                "runs": [r.to_dict() for r in runs],
                "environment_before": before,
                "environment_after": world.snapshot(),
            }
        )
        flag = "✓" if runs[0].status == case.expect.status else "·"
        print(f"  {flag} {case.id:<8} {case.title[:34]:<36} status={runs[0].status}")

    payload = {
        "meta": {
            "ran_at": datetime.now().isoformat(timespec="seconds"),
            "agent": args.agent,
            "cases_file": str(args.cases),
            "case_count": len(cases),
        },
        "results": records,
    }
    (out_dir / "run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n✅ 写出 {out_dir / 'run.json'}")
    print(f"   下一步：python eval/score.py --run \"{out_dir / 'run.json'}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
