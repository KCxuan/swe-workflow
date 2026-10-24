from typing import TypedDict, Any
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from .models import RequirementPlan, CodingResult, ReviewReport
from .tools import fingerprint


class InfrastructureError(RuntimeError):
    pass


class State(TypedDict, total=False):
    request: str
    feedback: str
    plan: dict
    plan_revision: int
    approved_revision: int | None
    coding_result: dict
    review_report: dict
    runtime: dict
    phase: str
    status: str
    issues: list[str]
    error: str
    startup_error: str
    repair_count: int
    total_repairs: int
    batch_id: int
    is_repair: bool
    allow_existing: bool
    retry: bool
    source_hash: str


def validate_report(plan, report, evidence, source_hash):
    expected = {c["id"]: c for c in plan["acceptance"]}
    results = report["results"]
    ids = [r["criterion_id"] for r in results]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ValueError("审查结果必须完整且唯一地对应确认的验收清单")
    problems = []
    for r in results:
        if r["status"] != "passed":
            problems.append(f"{r['criterion_id']}: {r['detail']}")
            continue
        refs = [evidence[e] for e in r["evidence_ids"] if e in evidence]
        refs = [e for e in refs if e["passed"] and e["source_hash"] == source_hash]
        kind = expected[r["criterion_id"]]["kind"]
        valid = False
        if kind == "code":
            valid = any(e["kind"] == "code" or (e["kind"] == "test" and e["assertion"]) for e in refs)
        elif kind in {"browser", "api"}:
            valid = any(e["kind"] == kind and e["assertion"] for e in refs)
        elif kind == "data":
            valid = any(e["kind"] == "data" and e["assertion"] and e["details"].get("expected") for e in refs)
            api = [e for e in refs if e["kind"] == "api" and e["assertion"]]
            valid = valid or (any(e["details"]["method"] in {"POST", "PUT", "PATCH", "DELETE"} for e in api)
                             and any(e["details"]["method"] == "GET" and e["details"].get("expected_json") for e in api))
        if not valid or len(refs) != len(r["evidence_ids"]):
            r["status"] = "failed"
            r["detail"] = "缺少本轮对应类型的有效运行证据；" + r["detail"]
            problems.append(f"{r['criterion_id']}: {r['detail']}")
    return problems + report["issues"]


def build_graph(checkpointer, agents, ctx, project_id):
    async def planner(state):
        plan = await agents.invoke("planner", state, ctx)
        return {"plan": plan, "plan_revision": state.get("plan_revision", 0) + 1,
                "approved_revision": None, "phase": "planning", "error": ""}

    def confirmation(state):
        decision = interrupt({"kind": "confirmation", "plan": state["plan"], "revision": state["plan_revision"]})
        if decision["kind"] == "revise":
            return {"feedback": decision.get("feedback", ""), "approved_revision": None}
        if decision["kind"] == "end":
            return {"status": "ended"}
        plan = RequirementPlan.model_validate(decision.get("plan") or state["plan"]).model_dump()
        revision = state["plan_revision"] + int(plan != state["plan"])
        return {"plan": plan, "plan_revision": revision, "approved_revision": revision,
                "status": "running", "feedback": ""}

    async def coder(state):
        if state.get("approved_revision") != state.get("plan_revision"):
            raise ValueError("需求版本尚未确认")
        if not state.get("allow_existing") and any(ctx.root.iterdir()):
            raise ValueError("目录在等待确认期间出现文件，请选择空目录后重新建立项目")
        await ctx.processes.stop_all()
        result = await agents.invoke("coder", state, ctx)
        return {"coding_result": result, "allow_existing": True, "review_report": {},
                "startup_error": "", "error": "", "phase": "coding"}

    async def prepare(state):
        coding = CodingResult.model_validate(state["coding_result"])
        for command in coding.setup_commands:
            ctx.emit("setup.started", {"program": command.program, "args": command.args})
            try:
                result = await ctx.processes.command(command, ctx.root, timeout=240)
            except (OSError, TimeoutError) as exc:
                raise InfrastructureError(f"环境准备失败：{exc}") from exc
            if result["exit_code"] != 0:
                if command.kind == "build":
                    return {"runtime": {}, "startup_error": "项目构建失败：" + result["output"][-12000:], "phase": "preparing"}
                raise InfrastructureError("依赖安装失败：" + result["output"][-12000:])
        try:
            runtime = await ctx.processes.start(project_id, ctx.run_id, ctx.root, coding, ctx.emit)
            return {"runtime": runtime, "phase": "preparing"}
        except (RuntimeError, OSError) as exc:
            return {"runtime": {}, "startup_error": str(exc), "phase": "preparing"}

    async def reviewer(state):
        ctx.review_hash = fingerprint(ctx.root)
        ctx.urls = state["runtime"]["urls"]
        ctx.checks = CodingResult.model_validate(state["coding_result"]).checks
        report = await agents.invoke("reviewer", state, ctx)
        if fingerprint(ctx.root) != ctx.review_hash:
            raise ValueError("检查期间源码发生变化，本轮报告不可用于完成判定")
        return {"review_report": report, "source_hash": ctx.review_hash, "phase": "reviewing"}

    def evaluate(state):
        if state.get("startup_error"):
            report = {"summary": "应用尚未启动成功", "issues": [state["startup_error"]], "results": [
                {"criterion_id": c["id"], "status": "not_run", "detail": "应用启动失败，尚未执行", "evidence_ids": []}
                for c in state["plan"]["acceptance"]]}
            issues = report["issues"]
        else:
            report = ReviewReport.model_validate(state["review_report"]).model_dump()
            issues = validate_report(state["plan"], report, ctx.evidence, state["source_hash"])
        count = state.get("repair_count", 0) + int(state.get("is_repair", False))
        total = state.get("total_repairs", 0) + int(state.get("is_repair", False))
        ctx.emit("review.results", report)
        return {"review_report": report, "issues": issues, "repair_count": count, "total_repairs": total,
                "is_repair": False, "status": "completed" if not issues else "running", "phase": "evaluating"}

    def begin_repair(state):
        ctx.emit("repair.started", {"batch": state.get("batch_id", 1), "round": state.get("repair_count", 0) + 1})
        return {"is_repair": True, "phase": "repairing"}

    def repair_decision(state):
        decision = interrupt({"kind": "repair", "revision": state["plan_revision"], "issues": state["issues"],
                              "repair_count": state.get("repair_count", 0)})
        if decision["kind"] == "continue":
            return {"repair_count": 0, "batch_id": state.get("batch_id", 1) + 1, "status": "running"}
        if decision["kind"] == "revise":
            return {"feedback": decision.get("feedback", ""), "approved_revision": None,
                    "repair_count": 0, "total_repairs": 0, "batch_id": 1}
        return {"status": "ended"}

    def initial(state):
        if state.get("retry") and state.get("approved_revision") == state.get("plan_revision") and state.get("plan"):
            return "repair_decision" if state.get("repair_count", 0) >= 3 else "coder"
        return "planner"

    graph = StateGraph(State)
    for name, node in {"planner": planner, "confirmation": confirmation, "coder": coder, "prepare": prepare,
                       "reviewer": reviewer, "evaluate": evaluate, "begin_repair": begin_repair,
                       "repair_decision": repair_decision}.items():
        graph.add_node(name, node)
    graph.add_conditional_edges(START, initial)
    graph.add_edge("planner", "confirmation")
    graph.add_conditional_edges("confirmation", lambda s: END if s.get("status") == "ended" else "coder" if s.get("approved_revision") else "planner")
    graph.add_edge("coder", "prepare")
    graph.add_conditional_edges("prepare", lambda s: "evaluate" if s.get("startup_error") else "reviewer")
    graph.add_edge("reviewer", "evaluate")
    graph.add_conditional_edges("evaluate", lambda s: END if s["status"] == "completed" else "begin_repair" if s["repair_count"] < 3 else "repair_decision")
    graph.add_edge("begin_repair", "coder")
    graph.add_conditional_edges("repair_decision", lambda s: END if s.get("status") == "ended" else "planner" if not s.get("approved_revision") else "begin_repair")
    return graph.compile(checkpointer=checkpointer)
