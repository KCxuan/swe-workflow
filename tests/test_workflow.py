"""Offline flow / API tests. Fake roles do not establish real model quality."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from backend.app.config import Settings
from backend.app.manager import Manager, BusyError
from backend.app.models import DecisionInput, RequirementPlan, RunInput
from backend.app.tools import safe_path, fingerprint, ToolContext, Processes
from backend.app.workflow import validate_report

PLAN = {"title": "计数器", "summary": "按钮增加数字", "features": ["点击加一"], "pages": ["首页"],
        "tech_stack": "HTML", "acceptance": [{"id": "A1", "description": "点击加一后显示1", "kind": "browser"}]}
CODING = {"summary": "生成计数器", "changed_files": ["index.html"], "setup_commands": [], "checks": [],
          "services": [{"name": "web", "program": "python", "args": ["-m", "http.server", "12345"], "port": 12345, "preview": True}]}


class FakeAgents:
    def __init__(self):
        self.calls = []
        self.fail_checks = self.fail_plan = self.block = False
        self.setup_kind = None

    async def invoke(self, role, state, ctx):
        self.calls.append(role)
        if role == "planner":
            if self.fail_plan:
                raise RuntimeError("simulated model outage")
            return PLAN
        if role == "coder":
            if self.block:
                (ctx.root / "partial.txt").write_text("partial", encoding="utf-8")
                await asyncio.Event().wait()
            (ctx.root / "index.html").write_text("<button>+</button>", encoding="utf-8")
            return {**CODING, "setup_commands": [{"program": "npm", "args": ["run", "build"], "kind": self.setup_kind}] if self.setup_kind else []}
        e = ctx.record("browser", {"observed": "1"}, assertion=True)
        return {"summary": "simulated review", "issues": [], "results": [{"criterion_id": "A1",
            "status": "failed" if self.fail_checks else "passed", "detail": "simulated check", "evidence_ids": [e["id"]]}]}


class FakeProcesses:
    def __init__(self):
        self.fail_start = False
        self.services = {}
    async def stop_all(self):
        pass
    async def command(self, *args, **kwargs):
        return {"exit_code": 1, "output": "simulated build or download error"}
    async def start(self, *args):
        if self.fail_start:
            raise RuntimeError("simulated application syntax error")
        return {"urls": {"web": "http://127.0.0.1:12345"}, "preview_url": "http://127.0.0.1:12345"}


@pytest_asyncio.fixture
async def manager(tmp_path):
    m = Manager(Settings(root=tmp_path), FakeAgents())
    m.processes = FakeProcesses()
    yield m
    await m.close()


async def settled(manager):
    if manager.task:
        await asyncio.wait_for(asyncio.shield(manager.task), 10)


def request(manager, name="project", mode="new"):
    root = manager.settings.root / name
    root.mkdir(exist_ok=True)
    return RunInput(path=str(root), mode=mode, request="做一个计数器")


async def approved(manager):
    run = await manager.new(request(manager))
    await settled(manager)
    await manager.decide(run["id"], DecisionInput(kind="confirm"))
    await settled(manager)
    return manager.current


@pytest.mark.asyncio
async def test_new_rejects_nonempty_modify_keeps_existing_files(manager):
    value = request(manager)
    keep = Path(value.path) / "keep.txt"
    keep.write_text("keep")
    with pytest.raises(ValueError, match="空文件夹"):
        await manager.new(value)
    assert keep.read_text() == "keep"
    run = await manager.new(value.model_copy(update={"mode": "modify"}))
    await settled(manager)
    assert manager.run()["status"] == "waiting_confirmation"
    await manager.decide(run["id"], DecisionInput(kind="confirm"))
    await settled(manager)
    assert keep.read_text() == "keep"
    assert manager.run()["status"] == "completed"


def test_path_escape_and_duplicate_ids(tmp_path):
    with pytest.raises(ValueError, match="超出"):
        safe_path(tmp_path, "../outside")
    with pytest.raises(ValueError):
        RequirementPlan.model_validate({**PLAN, "acceptance": PLAN["acceptance"] * 2})


@pytest.mark.asyncio
async def test_external_runtime_is_not_a_development_target(manager, monkeypatch):
    import backend.app.manager as module
    local = manager.settings.root / "local-runtime"
    (local / ".venv").mkdir(parents=True)
    (local / ".cache").mkdir()
    monkeypatch.setattr(module, "LOCAL_HOME", local)
    for root in [local, local / ".venv", local / ".cache"]:
        with pytest.raises(ValueError, match="工具自身"):
            await manager.new(RunInput(path=str(root), mode="modify", request="edit"))


@pytest.mark.parametrize("evidence", [{}, {"E": {"passed": True, "kind": "browser", "assertion": True, "source_hash": "old"}}])
def test_missing_or_old_evidence_cannot_pass(evidence):
    report = {"issues": [], "results": [{"criterion_id": "A1", "status": "passed", "detail": "claim", "evidence_ids": ["E"]}]}
    assert validate_report(PLAN, report, evidence, "current")
    assert report["results"][0]["status"] == "failed"


def test_incomplete_report_rejected():
    with pytest.raises(ValueError):
        validate_report(PLAN, {"issues": [], "results": []}, {}, "current")


def test_actual_registered_test_is_code_evidence_only():
    report = {"issues": [], "results": [{"criterion_id": "A1", "status": "passed", "detail": "tested", "evidence_ids": ["E"]}]}
    evidence = {"E": {"kind": "test", "passed": True, "assertion": True, "source_hash": "current"}}
    plan = {**PLAN, "acceptance": [{**PLAN["acceptance"][0], "kind": "code"}]}
    assert not validate_report(plan, report, evidence, "current")
    assert validate_report(PLAN, report, evidence, "current")


def test_reports_and_database_data_do_not_change_source_fingerprint(tmp_path):
    (tmp_path / "app.py").write_text("a=1")
    first = fingerprint(tmp_path)
    (tmp_path / "data.db").write_bytes(b"changed")
    folder = tmp_path / ".workflow"
    folder.mkdir()
    (folder / "generated.py").write_text("report only")
    assert fingerprint(tmp_path) == first
    (tmp_path / "app.py").write_text("a=2")
    assert fingerprint(tmp_path) != first


@pytest.mark.asyncio
async def test_confirmation_prevents_writes_and_rejects_double_submission(manager):
    value = request(manager)
    run = await manager.new(value)
    await settled(manager)
    assert manager.run()["status"] == "waiting_confirmation"
    assert not list(Path(value.path).iterdir())
    with pytest.raises(BusyError):
        await manager.new(request(manager, "other"))
    await manager.decide(run["id"], DecisionInput(kind="confirm", plan=RequirementPlan.model_validate({**PLAN, "title": "edited"})))
    with pytest.raises(BusyError):
        await manager.decide(run["id"], DecisionInput(kind="confirm"))
    await settled(manager)
    assert manager.run()["state"]["plan"]["title"] == "edited"
    assert manager.agents.calls == ["planner", "coder", "reviewer"]


@pytest.mark.asyncio
async def test_success_saves_current_report_in_project(manager):
    run = await approved(manager)
    assert run["status"] == "completed"
    assert run["state"]["repair_count"] == 0
    report = json.loads(Path(run["report_path"]).read_text(encoding="utf-8"))
    assert report["status"] == "completed"
    assert report["evidence"][0]["assertion"]
    assert (Path(run["path"]) / ".workflow/execution.log").is_file()
    assert not (manager.settings.data / "app.db").exists()


@pytest.mark.asyncio
async def test_three_repairs_end_in_failure_without_another_batch(manager):
    manager.agents.fail_checks = True
    run = await approved(manager)
    assert run["status"] == "failed"
    assert run["state"]["repair_count"] == 3
    assert manager.agents.calls.count("coder") == 4
    assert sum(e["type"] == "repair.started" for e in manager.events) == 3
    assert json.loads(Path(run["report_path"]).read_text(encoding="utf-8"))["status"] == "failed"


@pytest.mark.asyncio
async def test_startup_failures_are_bounded(manager):
    manager.processes.fail_start = True
    run = await approved(manager)
    assert run["status"] == "failed"
    assert run["state"]["repair_count"] == 3
    assert all(r["status"] == "not_run" for r in run["state"]["review_report"]["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,repairs", [("build", 3), ("dependency", 0)])
async def test_build_repairs_but_download_failure_stops(manager, kind, repairs):
    manager.agents.setup_kind = kind
    run = await approved(manager)
    assert run["status"] == "failed"
    assert run["state"]["repair_count"] == repairs


@pytest.mark.asyncio
async def test_stop_retains_partial_source_and_releases_execution(manager):
    manager.agents.block = True
    run = await manager.new(request(manager))
    await settled(manager)
    await manager.decide(run["id"], DecisionInput(kind="confirm"))
    await asyncio.sleep(.06)
    stopped = await manager.stop(run["id"])
    assert stopped["status"] == "stopped"
    assert (Path(run["path"]) / "partial.txt").read_text() == "partial"
    assert stopped["report_path"]
    manager.agents.block = False
    other = await manager.new(request(manager, "other"))
    await settled(manager)
    assert manager.run()["id"] == other["id"]


@pytest.mark.asyncio
async def test_new_modify_task_uses_original_source_but_not_old_result(manager):
    old = await approved(manager)
    value = RunInput(path=old["path"], mode="modify", request="增加减一按钮")
    run = await manager.new(value)
    await settled(manager)
    assert run["id"] != old["id"]
    assert not manager.run()["state"].get("review_report")
    with pytest.raises(KeyError):
        manager.run(old["id"])
    assert (Path(old["path"]) / "index.html").is_file()


@pytest.mark.asyncio
async def test_decline_and_changed_directory_do_not_start_coder(manager):
    run = await manager.new(request(manager))
    await settled(manager)
    await manager.decide(run["id"], DecisionInput(kind="end"))
    await settled(manager)
    assert manager.run()["status"] == "ended" and manager.agents.calls == ["planner"]
    run = await manager.new(request(manager, "changed"))
    await settled(manager)
    (Path(run["path"]) / "keep.txt").write_text("keep")
    await manager.decide(run["id"], DecisionInput(kind="confirm"))
    await settled(manager)
    assert manager.run()["status"] == "failed"
    assert manager.agents.calls.count("coder") == 0
    assert (Path(run["path"]) / "keep.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_restart_does_not_read_or_change_legacy_database(tmp_path):
    old = tmp_path / ".data/app.db"
    old.parent.mkdir()
    old.write_bytes(b"legacy database left untouched")
    m = Manager(Settings(root=tmp_path), FakeAgents())
    assert m.current is None
    await m.close()
    assert old.read_bytes() == b"legacy database left untouched"


@pytest.mark.asyncio
async def test_new_task_and_shutdown_wait_for_report_finalization(manager):
    run = await manager.new(request(manager))
    await settled(manager)
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager.ctx.close
    async def close():
        if manager.current["status"] == "completed":
            entered.set()
            await release.wait()
        await original()
    manager.ctx.close = close
    await manager.decide(run["id"], DecisionInput(kind="confirm"))
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(BusyError):
        await manager.new(request(manager, "too-early"))
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(.02)
    assert not closing.done()
    release.set()
    await closing
    assert Path(manager.run()["report_path"]).is_file()


@pytest.mark.asyncio
async def test_api_current_report_events_and_origin(tmp_path):
    from backend.app.main import create_app
    app = create_app(Settings(root=tmp_path), FakeAgents())
    async with app.router.lifespan_context(app):
        m = app.state.manager
        m.processes = FakeProcesses()
        value = request(m)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as c:
            assert (await c.get("/api/current")).json() is None
            run = (await c.post("/api/runs", json=value.model_dump())).json()
            await settled(m)
            assert (await c.get("/api/current")).json()["status"] == "waiting_confirmation"
            assert (await c.post(f"/api/runs/{run['id']}/decision", json={"kind": "continue"})).status_code == 422
            assert (await c.post(f"/api/runs/{run['id']}/decision", json={"kind": "confirm"})).status_code == 202
            await settled(m)
            assert (await c.get(f"/api/runs/{run['id']}/report")).json()["status"] == "completed"
            events = await c.get(f"/api/runs/{run['id']}/events")
            assert "run.finished" in events.text and "event: closed" in events.text
            replay = await c.get(f"/api/runs/{run['id']}/events", params={"after": len(m.events)})
            assert "run.started" not in replay.text and "event: closed" in replay.text
            assert (await c.get("/api/projects")).status_code == 404
            assert (await c.post("/api/runs", headers={"Origin": "https://unrelated.example"}, json=value.model_dump())).status_code == 403


@pytest.mark.asyncio
async def test_coder_command_schema_and_real_subprocess(tmp_path):
    settings = Settings(root=tmp_path)
    ctx = ToolContext(tmp_path, "command-test", settings, Processes(settings), lambda *args: None)
    command = next(t for t in ctx.tools("coder") if t.name == "run_command")
    assert "arguments" in command.args_schema.model_json_schema()["properties"]
    result = await command.ainvoke({"program": "python", "arguments": ["-c", "print(17+25)"], "cwd": "."})
    assert result["passed"] and result["details"]["output"].strip() == "42"
