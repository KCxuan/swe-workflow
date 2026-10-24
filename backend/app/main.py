import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .config import Settings, ROOT
from .manager import Manager, BusyError, TERMINAL
from .models import ProjectInput, RunInput, DecisionInput
from .store import Store
from .tools import files, safe_path, fingerprint


def create_app(settings=None, agents=None):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        settings.data.mkdir(parents=True, exist_ok=True)
        store = Store(settings.data / "app.db")
        async with AsyncSqliteSaver.from_conn_string(str(settings.data / "checkpoints.db")) as saver:
            await saver.setup()
            manager = Manager(store, settings, saver, agents)
            manager.processes.recover_orphans()
            app.state.manager = manager
            app.state.store = store
            yield
            await manager.close()
        store.close()

    app = FastAPI(title="工序 · Agent 工作台", lifespan=lifespan)

    def present(run):
        project = app.state.store.project(run["project_id"])
        checked_hash = run["state"].get("source_hash")
        run["report_current"] = bool(checked_hash and fingerprint(Path(project["path"])) == checked_hash)
        run["preview_url"] = next((f"http://127.0.0.1:{s['spec'].port}"
            for s in app.state.manager.processes.services.values()
            if s["project_id"] == project["id"] and s["spec"].preview and s["proc"].returncode is None), None)
        return run
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "Last-Event-ID"])

    @app.middleware("http")
    async def local_origin(request, call_next):
        # Local tools can execute code: don't let an arbitrary website issue browser requests.
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api/") and origin and origin not in {
            "http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8765", "http://localhost:8765"
        }:
            return JSONResponse({"detail": "不接受此页面来源"}, status_code=403)
        if request.url.path.startswith("/api/") and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不接受跨站请求"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": settings.redact(str(exc))}, status_code=409 if isinstance(exc, BusyError) else 400)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(_, exc):
        return JSONResponse({"detail": "项目已登记或已有未结束任务，请从历史项目打开"}, status_code=409)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "model": settings.model, "configured": bool(settings.api_key and settings.base_url),
                "active_run_id": app.state.manager.active_id}

    @app.post("/api/pick-directory")
    async def pick_directory():
        def pick():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                return filedialog.askdirectory(title="选择新项目的空文件夹")
            finally:
                root.destroy()
        try:
            return {"path": await asyncio.to_thread(pick)}
        except Exception:
            raise HTTPException(503, "目录选择器不可用，请输入文件夹的绝对路径")

    @app.post("/api/projects", status_code=201)
    async def create_project(value: ProjectInput):
        root = Path(value.path).expanduser().resolve()
        if root == settings.root.resolve() or settings.root.resolve().is_relative_to(root):
            raise ValueError("不能把工作流工具自身或其上级目录作为开发目录")
        return app.state.store.create_project(value.path, value.name)

    @app.get("/api/projects")
    async def projects():
        return app.state.store.projects()

    @app.get("/api/projects/{project_id}")
    async def project(project_id: str):
        return {**app.state.store.project(project_id), "runs": [present(r) for r in app.state.store.runs(project_id)]}

    @app.post("/api/projects/{project_id}/runs", status_code=202)
    async def new_run(project_id: str, value: RunInput):
        return await app.state.manager.new(project_id, value.request)

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str):
        return present(app.state.store.run(run_id))

    @app.get("/api/runs/{run_id}/evidence")
    async def evidence(run_id: str):
        app.state.store.run(run_id)
        p = settings.data / "runs" / run_id / "evidence.jsonl"
        if not p.is_file():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

    @app.post("/api/runs/{run_id}/decision", status_code=202)
    async def decision(run_id: str, value: DecisionInput):
        return await app.state.manager.decide(run_id, value)

    @app.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str):
        return await app.state.manager.stop(run_id)

    @app.post("/api/runs/{run_id}/retry", status_code=202)
    async def retry(run_id: str):
        return await app.state.manager.retry(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        app.state.store.run(run_id)
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "无效的事件编号")
        async def stream():
            cursor = after
            while not await request.is_disconnected():
                rows = app.state.store.events(run_id, cursor)
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not rows and app.state.store.run(run_id)["status"] in TERMINAL:
                    yield "event: closed\ndata: {}\n\n"
                    break
                if not rows:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/projects/{project_id}/files")
    async def project_files(project_id: str):
        return files(Path(app.state.store.project(project_id)["path"]))

    @app.get("/api/projects/{project_id}/files/content")
    async def file_content(project_id: str, path: str):
        p = safe_path(Path(app.state.store.project(project_id)["path"]), path)
        if not p.is_file() or p.stat().st_size > 500000:
            raise ValueError("文件不存在或超过500KB")
        return {"path": path, "content": p.read_text(encoding="utf-8", errors="replace")}

    @app.get("/api/runs/{run_id}/artifacts/{name}")
    async def artifact(run_id: str, name: str):
        app.state.store.run(run_id)
        if not re.fullmatch(r"[a-f0-9]+\.png", name):
            raise HTTPException(404)
        p = settings.data / "runs" / run_id / name
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p)

    @app.post("/api/projects/{project_id}/preview")
    async def preview(project_id: str):
        return await app.state.manager.preview(project_id)

    @app.delete("/api/projects/{project_id}/preview")
    async def stop_preview(project_id: str):
        return await app.state.manager.preview(project_id, stop=True)

    dist = settings.root / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="workbench")
    return app


app = create_app()
