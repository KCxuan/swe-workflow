import asyncio
import json
from pathlib import Path

from langgraph.types import Command

from .agents import Agents, AgentOutputError
from .models import CodingResult
from .tools import ToolContext, Processes
from .workflow import build_graph

TERMINAL = {"completed", "failed", "stopped", "interrupted", "ended"}


class BusyError(ValueError):
    pass


class Manager:
    def __init__(self, store, settings, checkpointer, agents=None):
        self.store, self.settings, self.checkpointer = store, settings, checkpointer
        self.agents = agents or Agents(settings)
        self.processes = Processes(store, settings)
        self.lock = asyncio.Lock()
        self.active_id = None
        self.task = None

    def emit(self, run_id, kind, payload):
        payload = json.loads(self.settings.redact(json.dumps(payload, ensure_ascii=False)))
        if kind == "agent.started":
            self.store.update(run_id, phase=payload["role"])
        self.store.event(run_id, kind, payload)

    def free(self):
        if self.active_id:
            raise BusyError("另一个任务正在执行，请等待或先停止当前任务")

    async def new(self, project_id, request):
        async with self.lock:
            self.free()
            project = self.store.project(project_id)
            prior = self.store.runs(project_id)
            if any(r["status"] not in TERMINAL for r in prior):
                raise BusyError("该项目已有未结束任务，请先处理确认或结束该任务")
            allow = any(r["state"].get("approved_revision") for r in prior)
            state = {"request": request, "plan_revision": 0, "approved_revision": None,
                     "repair_count": 0, "total_repairs": 0, "batch_id": 1, "is_repair": False,
                     "status": "running", "phase": "planning", "allow_existing": bool(allow), "retry": False}
            run = self.store.create_run(project_id, request, state)
            self.launch(run["id"], state)
            return self.store.run(run["id"])

    def launch(self, run_id, value):
        self.active_id = run_id
        self.store.update(run_id, status="running")
        self.task = asyncio.create_task(self.drive(run_id, value))

    async def decide(self, run_id, decision):
        async with self.lock:
            run = self.store.run(run_id)
            value = decision.model_dump(exclude_none=True)
            old = self.store.decision(run_id, decision.decision_id)
            if old is not None:
                if old != value:
                    raise ValueError("相同决定ID不能提交不同内容")
                return run
            self.free()
            if run["status"] not in {"waiting_confirmation", "waiting_repair"}:
                raise ValueError("当前任务没有等待此决定")
            if decision.expected_revision != run["state"].get("plan_revision"):
                raise ValueError("提案已更新，请刷新后确认当前版本")
            allowed = {"confirm", "revise", "end"} if run["status"] == "waiting_confirmation" else {"continue", "revise", "end"}
            if decision.kind not in allowed:
                raise ValueError("当前阶段不接受该决定")
            self.store.decision(run_id, decision.decision_id, value)
            self.launch(run_id, Command(resume=value))
            return self.store.run(run_id)

    async def retry(self, run_id):
        async with self.lock:
            self.free()
            old = self.store.run(run_id)
            if old["status"] not in {"failed", "stopped", "interrupted"}:
                raise ValueError("只可重试失败、停止或中断的任务")
            state = {**old["state"], "status": "running", "error": "", "startup_error": "", "retry": True,
                     "allow_existing": bool(old["state"].get("approved_revision"))}
            run = self.store.create_run(old["project_id"], old["request"], state, parent_id=run_id)
            self.launch(run["id"], state)
            return run

    async def drive(self, run_id, value):
        run = self.store.run(run_id)
        project = self.store.project(run["project_id"])
        ctx = ToolContext(Path(project["path"]), run_id, self.settings, self.processes,
                          lambda kind, payload: self.emit(run_id, kind, payload))
        graph = build_graph(self.checkpointer, self.agents, ctx, project["id"])
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 160}
        try:
            self.emit(run_id, "run.started", {"project": project["name"]})
            async for update in graph.astream(value, config, stream_mode="updates", durability="sync"):
                snapshot = await graph.aget_state(config)
                state = dict(snapshot.values)
                if "__interrupt__" in update:
                    payload = update["__interrupt__"][0].value
                    status = "waiting_confirmation" if payload["kind"] == "confirmation" else "waiting_repair"
                    self.store.update(run_id, state=state, status=status, phase=status)
                    self.emit(run_id, "run.waiting", payload)
                else:
                    self.store.update(run_id, state=state, phase=state.get("phase", "running"))
                    for name in update:
                        self.emit(run_id, "node.finished", {"node": name})
            snapshot = await graph.aget_state(config)
            state = dict(snapshot.values)
            if self.store.run(run_id)["status"] not in {"waiting_confirmation", "waiting_repair"}:
                status = state.get("status", "failed")
                if status not in {"completed", "ended"}:
                    status = "failed"
                self.store.update(run_id, state=state, status=status)
                self.emit(run_id, "run.finished", {"status": status})
                if status != "completed":
                    await self.processes.stop_all(project["id"])
        except asyncio.CancelledError:
            await self.processes.stop_all(project["id"])
            self.store.update(run_id, status="stopped")
            self.emit(run_id, "run.finished", {"status": "stopped"})
        except Exception as exc:
            await self.processes.stop_all(project["id"])
            state = self.store.run(run_id)["state"]
            error = self.settings.redact(str(exc) if isinstance(exc, AgentOutputError) else f"{type(exc).__name__}: {exc}")
            self.store.update(run_id, status="failed", state={**state, "error": error})
            self.emit(run_id, "run.failed", {"error": error})
        finally:
            await ctx.close()
            if self.active_id == run_id:
                self.active_id = None

    async def stop(self, run_id):
        async with self.lock:
            run = self.store.run(run_id)
            if self.active_id == run_id and self.task:
                self.store.update(run_id, status="stopping")
                self.task.cancel()
                task = self.task
            elif run["status"] not in TERMINAL:
                self.store.update(run_id, status="stopped")
                await self.processes.stop_all(run["project_id"])
                return self.store.run(run_id)
            else:
                return run
        await asyncio.shield(task)
        return self.store.run(run_id)

    async def preview(self, project_id, stop=False):
        async with self.lock:
            self.free()
            project = self.store.project(project_id)
            if stop:
                await self.processes.stop_all(project_id)
                return {"preview_url": None}
            runs = self.store.runs(project_id)
            candidate = next((r for r in runs if r["state"].get("coding_result")), None)
            if not candidate:
                raise ValueError("该项目尚无启动方案")
            return await self.processes.start(project_id, candidate["id"], Path(project["path"]),
                       CodingResult.model_validate(candidate["state"]["coding_result"]),
                       lambda kind, payload: self.emit(candidate["id"], kind, payload))

    async def close(self):
        if self.active_id and self.task:
            await self.stop(self.active_id)
        await self.processes.stop_all()
