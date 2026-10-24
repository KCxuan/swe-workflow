import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .manager import Manager, BusyError, TERMINAL
from .models import RunInput, DecisionInput
from .tools import safe_path


def create_app(settings=None, agents=None):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = Manager(settings, agents)
        try:
            yield
        finally:
            await app.state.manager.close()

    app = FastAPI(title="工序 · 软件开发 Workflow", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type", "Last-Event-ID"])

    @app.middleware("http")
    async def local_origin(request, call_next):
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api/") and ((origin and origin not in {
            "http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8765", "http://localhost:8765"
        }) or request.headers.get("sec-fetch-site") == "cross-site"):
            return JSONResponse({"detail": "不接受此页面来源"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": settings.redact(str(exc))}, status_code=409 if isinstance(exc, BusyError) else 400)

    @app.get("/api/health")
    async def health():
        run = app.state.manager.current
        return {"ok": True, "model": settings.model, "configured": bool(settings.api_key and settings.base_url),
                "active_run_id": run["id"] if run and run["status"] in {"running", "stopping"} else None}

    @app.post("/api/pick-directory")
    async def pick_directory():
        def pick():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                return filedialog.askdirectory(title="选择本次开发的文件夹")
            finally:
                root.destroy()
        try:
            return {"path": await asyncio.to_thread(pick)}
        except Exception:
            raise HTTPException(503, "目录选择器不可用，请输入文件夹的绝对路径")

    @app.get("/api/current")
    async def current():
        return app.state.manager.present() if app.state.manager.current else None

    @app.post("/api/runs", status_code=202)
    async def new_run(value: RunInput):
        return await app.state.manager.new(value)

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str):
        app.state.manager.run(run_id)
        return app.state.manager.present()

    @app.post("/api/runs/{run_id}/decision", status_code=202)
    async def decision(run_id: str, value: DecisionInput):
        return await app.state.manager.decide(run_id, value)

    @app.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str):
        return await app.state.manager.stop(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        manager = app.state.manager
        task = manager.run(run_id)
        records = manager.events
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "无效的事件编号")
        async def stream():
            cursor = after
            while not await request.is_disconnected():
                rows = [e for e in records if e["id"] > cursor]
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not rows and task["status"] in TERMINAL:
                    yield "event: closed\ndata: {}\n\n"
                    break
                if not rows:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(.4)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/runs/{run_id}/report")
    async def report(run_id: str):
        run = app.state.manager.run(run_id)
        if not run["report_path"]:
            raise HTTPException(404, "本次任务尚未保存报告")
        return FileResponse(run["report_path"], media_type="application/json", filename="workflow-report.json")

    @app.get("/api/runs/{run_id}/screenshots/{name}")
    async def screenshot(run_id: str, name: str):
        run = app.state.manager.run(run_id)
        if name not in run["screenshots"] or not re.fullmatch(r"check-[1-3]\.png", name):
            raise HTTPException(404)
        return FileResponse(safe_path(Path(run["path"]), ".workflow/" + name))

    @app.post("/api/runs/{run_id}/preview")
    async def preview(run_id: str):
        return await app.state.manager.preview(run_id)

    @app.delete("/api/runs/{run_id}/preview")
    async def stop_preview(run_id: str):
        return await app.state.manager.preview(run_id, stop=True)

    dist = settings.root / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="workbench")
    return app


app = create_app()
