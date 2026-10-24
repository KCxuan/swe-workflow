"""Bounded live probe: a real tool round-trip and validated structured output."""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import tool
from pydantic import BaseModel
from backend.app.config import Settings
from backend.app.agents import Agents
from backend.app.tools import ToolContext, Processes


class Answer(BaseModel):
    result: int


async def command_probe(settings):
    root = settings.data / "protocol-probe" / "project"
    root.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(root, "protocol-probe", settings, Processes(settings), lambda *args: None)
    try:
        command = next(t for t in ctx.tools("coder") if t.name == "run_command")
        agent = create_agent(Agents(settings).model().bind(max_tokens=1000), tools=[command],
            system_prompt="Call run_command to run Python with arguments ['-c', 'print(17+25)']. Use the actual output as result. Return structured output.",
            response_format=ToolStrategy(Answer))
        output = await asyncio.wait_for(agent.ainvoke({"messages": [{"role": "user", "content": "Run the actual Python command and report the integer output."}]},
                         {"recursion_limit": 12}), 120)
        evidence = [e for e in ctx.evidence.values() if e["kind"] == "command"]
        valid = any(e["passed"] and e["details"]["exit_code"] == 0 and e["details"]["output"].strip() == "42" for e in evidence)
        return {"passed": valid and output["structured_response"].result == 42, "actual_command_results": [e["details"] for e in evidence]}
    finally:
        await ctx.close()


async def main():
    settings = Settings.load()
    called = []

    @tool
    def add(a: int, b: int) -> int:
        """Add two integers using an actual local tool."""
        called.append([a, b])
        return a + b

    agent = create_agent(Agents(settings).model().bind(max_tokens=1000), tools=[add],
        system_prompt="You must call add to calculate 17+25. Then return the result using the structured output tool.",
        response_format=ToolStrategy(Answer))
    try:
        result = await asyncio.wait_for(agent.ainvoke({"messages": [{"role": "user", "content": "Calculate 17+25 using the add tool."}]},
                      {"recursion_limit": 12}), timeout=120)
        answer = Answer.model_validate(result["structured_response"])
        report = {"model": settings.model, "actual_tool_calls": called, "answer": answer.model_dump(),
                  "passed": bool(called) and answer.result == 42}
        report["command_probe"] = await command_probe(settings)
        report["passed"] = report["passed"] and report["command_probe"]["passed"]
    except Exception as exc:
        report = {"passed": False, "error": settings.redact(f"{type(exc).__name__}: {exc}")}
    settings.data.mkdir(exist_ok=True)
    (settings.data / "model-probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
