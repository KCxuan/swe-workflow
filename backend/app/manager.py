import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from .agents import Agents, AgentOutputError
from .config import LOCAL_HOME
from .models import CodingResult
from .tools import ToolContext, Processes, safe_path, uid
from .workflow import build_graph

TERMINAL = {"completed", "failed", "stopped", "ended"}


class BusyError(ValueError):
    pass


class Manager:
    """Own exactly one current task and its application processes."""

    def __init__(self, settings, agents=None):
        self.settings = settings
        self.agents = agents or Agents(settings)
        self.processes = Processes(settings)
        self.lock = asyncio.Lock()
        self.current = None
        self.events = []
        self.task = self.graph = self.ctx = self.temp = None

    def run(self, run_id=None):
        if self.current is None or (run_id and run_id != self.current["id"]):
            raise KeyError("当前会话没有此任务，请重新提交需求")
        return self.current

    def present(self):
        run = self.run()
        preview = next((f"http://127.0.0.1:{s['spec'].port}" for s in self.processes.services.values()
                        if s["spec"].preview and s["proc"].returncode is None), None)
        return {**run, "preview_url": preview}

    def emit(self, kind, payload):
        payload = json.loads(self.settings.redact(json.dumps(payload, ensure_ascii=False)))
        if kind == "agent.started":
            self.current["phase"] = payload["role"]
        self.events.append({"id": len(self.events) + 1, "type": kind, "payload": payload,
                            "created": datetime.now(timezone.utc).isoformat()})

    async def new(self, value):
        async with self.lock:
            if (self.task and not self.task.done()) or (self.current and self.current["status"] not in TERMINAL):
                raise BusyError("请先确认或停止当前任务")
            root = Path(value.path).expanduser()
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("请选择已经存在的文件夹，并使用绝对路径")
            root = root.resolve()
            tool = self.settings.root.resolve()
            if root == tool or tool.is_relative_to(root) or any(root.is_relative_to(tool / n) for n in
                ("backend", "frontend", "scripts", "tests", "docs", ".venv", ".cache", ".git")) or root == LOCAL_HOME or any(
                    root.is_relative_to(LOCAL_HOME / n) for n in (".venv", ".cache", "git")):
                raise ValueError("不能选择工作流工具自身的代码、依赖或上级目录")
            if value.mode == "new" and any(root.iterdir()):
                raise ValueError("新建项目需要空文件夹；已有源码请选择继续修改")
            await self.processes.stop_all()
            if self.temp:
                self.temp.cleanup()
            self.temp = TemporaryDirectory(prefix="swe-workflow-")
            self.events = []
            self.current = {"id": uid(), "path": str(root), "mode": value.mode, "request": value.request,
                            "status": "running", "phase": "planning", "approved": False,
                            "report_path": None, "screenshots": [],
                            "state": {"request": value.request, "repair_count": 0, "is_repair": False, "status": "running"}}
            self.ctx = ToolContext(root, self.current["id"], self.settings, self.processes, self.emit, self.temp.name)
            self.graph = build_graph(InMemorySaver(), self.agents, self.ctx, value.mode)
            self.launch(dict(self.current["state"]))
            return self.present()

    def launch(self, value):
        self.current["status"] = "running"
        self.task = asyncio.create_task(self.drive(value))

    async def decide(self, run_id, decision):
        async with self.lock:
            run = self.run(run_id)
            if run["status"] != "waiting_confirmation" or (self.task and not self.task.done()):
                raise BusyError("当前任务没有等待确认，请勿重复提交")
            run["approved"] = decision.kind == "confirm"
            self.launch(Command(resume=decision.model_dump(exclude_none=True)))
            return self.present()

    async def drive(self, value):
        run = self.current
        try:
            self.emit("run.started", {})
            config = {"configurable": {"thread_id": run["id"]}, "recursion_limit": 100}
            async for update in self.graph.astream(value, config, stream_mode="updates", durability="sync"):
                if "__interrupt__" in update:
                    run["status"] = run["phase"] = "waiting_confirmation"
                    self.emit("run.waiting", {"kind": "confirmation"})
                else:
                    for node, changes in update.items():
                        run["state"].update(changes)
                        run["phase"] = run["state"].get("phase", run["phase"])
                        self.emit("node.finished", {"node": node})
            if run["status"] == "waiting_confirmation":
                return
            run["status"] = run["state"].get("status", "failed")
            if run["status"] not in TERMINAL:
                run["status"] = "failed"
        except asyncio.CancelledError:
            run["status"] = "stopped"
        except Exception as exc:
            run["status"] = "failed"
            run["state"]["error"] = self.settings.redact(str(exc) if isinstance(exc, AgentOutputError) else f"{type(exc).__name__}: {exc}")
            self.emit("run.failed", {"error": run["state"]["error"]})
        finally:
            if run["status"] != "waiting_confirmation" or (self.task and not self.task.done()):
                if run["status"] != "completed":
                    await self.processes.stop_all()
                await self.ctx.close()
                if run["approved"]:
                    try:
                        self.save_report()
                    except (OSError, ValueError) as exc:
                        run["status"] = "failed"
                        run["state"]["error"] = self.settings.redact(f"保存报告失败：{exc}")
                        await self.processes.stop_all()
                self.emit("run.finished", {"status": run["status"]})

    def save_report(self):
        run = self.current
        folder = safe_path(self.ctx.root, ".workflow")
        folder.mkdir(exist_ok=True)
        report = run["state"].get("review_report", {})
        refs = {e for result in report.get("results", []) for e in result["evidence_ids"]}
        evidence = [e for key, e in self.ctx.evidence.items() if key in refs]
        images = list(dict.fromkeys(e["details"]["screenshot"] for e in reversed(evidence) if e["details"].get("screenshot")))[:3]
        for i in range(1, 4):
            old = safe_path(self.ctx.root, f".workflow/check-{i}.png")
            if old.is_file():
                old.unlink()
        run["screenshots"] = []
        image_names = {}
        for i, name in enumerate(images, 1):
            source = self.ctx.artifacts / name
            if source.is_file():
                filename = f"check-{i}.png"
                shutil.copyfile(source, folder / filename)
                run["screenshots"].append(filename)
                image_names[name] = filename
        evidence = json.loads(json.dumps(evidence, ensure_ascii=False))
        for item in evidence:
            original = item["details"].pop("screenshot", None)
            if original in image_names:
                item["details"]["screenshot"] = image_names[original]
        # The saved report describes this execution, never a previous project's verdict.
        output = {"task_id": run["id"], "status": run["status"], "request": run["request"], **run["state"],
                  "evidence": evidence, "screenshots": run["screenshots"]}
        output["status"] = run["status"]
        target = safe_path(self.ctx.root, ".workflow/report.json")
        target.write_text(self.settings.redact(json.dumps(output, ensure_ascii=False, indent=2)), encoding="utf-8")
        safe_path(self.ctx.root, ".workflow/execution.log").write_text(
            "\n".join(f"{e['created']} {e['type']} {json.dumps(e['payload'], ensure_ascii=False)}" for e in self.events), encoding="utf-8")
        run["report_path"] = str(target)

    async def stop(self, run_id):
        async with self.lock:
            run = self.run(run_id)
            if run["status"] in TERMINAL:
                return self.present()
            if run["status"] == "waiting_confirmation":
                run["status"] = "stopped"
                await self.ctx.close()
                self.emit("run.finished", {"status": "stopped"})
                return self.present()
            if run["status"] != "stopping":
                run["status"] = "stopping"
                self.task.cancel()
            task = self.task
        await asyncio.shield(task)
        return self.present()

    async def preview(self, run_id, stop=False):
        async with self.lock:
            run = self.run(run_id)
            if run["status"] in {"running", "stopping"} or (self.task and not self.task.done()):
                raise BusyError("执行期间由工作流管理应用，请等待或先停止任务")
            if stop:
                await self.processes.stop_all()
            else:
                if not run["state"].get("coding_result"):
                    raise ValueError("当前任务尚无启动方案")
                await self.processes.start(Path(run["path"]), CodingResult.model_validate(run["state"]["coding_result"]), self.emit)
            return self.present()

    async def close(self):
        if self.current and self.current["status"] not in TERMINAL:
            await self.stop(self.current["id"])
        if self.task and not self.task.done():
            await asyncio.shield(self.task)
        await self.processes.stop_all()
        if self.temp:
            self.temp.cleanup()
