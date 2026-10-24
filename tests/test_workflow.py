"""Offline state-machine tests. Simulated agents do not prove live model quality."""
import asyncio
from pathlib import Path
import pytest
import pytest_asyncio
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from backend.app.config import Settings
from backend.app.manager import Manager, BusyError
from backend.app.models import DecisionInput, RequirementPlan
from backend.app.store import Store
from backend.app.tools import safe_path, fingerprint
from backend.app.workflow import validate_report

PLAN = {"title": "计数器", "summary": "按钮增加数字", "features": ["点击加一"], "pages": ["首页"],
        "tech_stack": "HTML", "acceptance": [{"id": "A1", "description": "点击加一后显示1", "kind": "browser"}]}
CODING = {"summary": "生成计数器", "changed_files": ["index.html"], "setup_commands": [], "checks": [],
          "services": [{"name": "web", "program": "python", "args": ["-m", "http.server", "12345"], "port": 12345, "preview": True}]}


class FakeAgents:
    def __init__(self, fail_checks=False, fail_plan=False, block=False):
        self.calls = []
        self.fail_checks, self.fail_plan, self.block = fail_checks, fail_plan, block

    async def invoke(self, role, state, ctx):
        self.calls.append(role)
        if role == "planner":
            if self.fail_plan:
                self.fail_plan = False
                raise RuntimeError("simulated model outage")
            return PLAN
        if role == "coder":
            if self.block:
                await asyncio.Event().wait()
            (ctx.root / "index.html").write_text("<button>+</button>", encoding="utf-8")
            return CODING
        e = ctx.record("browser", {"observed": "1"}, assertion=True)
        return {"summary": "simulated review", "issues": [], "results": [{"criterion_id": "A1",
            "status": "failed" if self.fail_checks else "passed", "detail": "simulated check", "evidence_ids": [e["id"]]}]}


class FakeProcesses:
    def __init__(self):
        self.fail_start = False
        self.services = {}
    async def stop_all(self, project_id=None):
        pass
    async def command(self, *args, **kwargs):
        return {"exit_code": 1, "output": "simulated build error"}
    async def start(self, *args):
        if self.fail_start:
            raise RuntimeError("simulated application syntax error")
        return {"urls": {"web": "http://127.0.0.1:12345"}, "preview_url": "http://127.0.0.1:12345"}


@pytest_asyncio.fixture
async def manager(tmp_path):
    settings = Settings(root=tmp_path)
    store = Store(settings.data / "app.db")
    agents = FakeAgents()
    async with AsyncSqliteSaver.from_conn_string(str(settings.data / "checkpoints.db")) as saver:
        await saver.setup()
        m = Manager(store, settings, saver, agents)
        m.processes = FakeProcesses()
        yield m
        await m.close()
    store.close()


async def settled(manager):
    if manager.task:
        await asyncio.wait_for(asyncio.shield(manager.task), 10)


def new_project(manager, name="p"):
    p = manager.settings.root / name
    p.mkdir()
    return manager.store.create_project(str(p), name)


async def approved(manager):
    project = new_project(manager)
    run = await manager.new(project["id"], "做一个计数器")
    await settled(manager)
    run = manager.store.run(run["id"])
    await manager.decide(run["id"], DecisionInput(kind="confirm", expected_revision=run["state"]["plan_revision"], decision_id="confirm"))
    await settled(manager)
    return project, manager.store.run(run["id"])


def test_nonempty_and_path_escape(tmp_path):
    store = Store(tmp_path / "state" / "app.db")
    p = tmp_path / "project"
    p.mkdir()
    (p / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="空文件夹"):
        store.create_project(str(p), "existing")
    assert (p / "keep.txt").read_text() == "keep"
    with pytest.raises(ValueError, match="超出"):
        safe_path(p, "../outside")
    store.close()


def test_plan_duplicate_ids():
    with pytest.raises(ValueError):
        RequirementPlan.model_validate({**PLAN, "acceptance": PLAN["acceptance"] * 2})


@pytest.mark.parametrize("evidence", [{}, {"E": {"passed": True, "kind": "browser", "assertion": True, "source_hash": "old"}}])
def test_unverified_reports_cannot_pass(evidence):
    report = {"issues": [], "results": [{"criterion_id": "A1", "status": "passed", "detail": "claim", "evidence_ids": ["E"]}]}
    assert validate_report(PLAN, report, evidence, "current")
    assert report["results"][0]["status"] == "failed"


def test_missing_report_item_rejected():
    with pytest.raises(ValueError):
        validate_report(PLAN, {"issues": [], "results": []}, {}, "hash")


def test_registered_test_is_valid_code_evidence_but_not_browser_evidence():
    report = {"issues": [], "results": [{"criterion_id": "A1", "status": "passed", "detail": "test passed", "evidence_ids": ["E"]}]}
    evidence = {"E": {"kind": "test", "passed": True, "assertion": True, "source_hash": "current"}}
    code_plan = {**PLAN, "acceptance": [{**PLAN["acceptance"][0], "kind": "code"}]}
    assert not validate_report(code_plan, report, evidence, "current")
    assert validate_report(PLAN, report, evidence, "current")


def test_fingerprint_ignores_database_but_not_code(tmp_path):
    (tmp_path / "app.py").write_text("a=1")
    first = fingerprint(tmp_path)
    (tmp_path / "app.db").write_bytes(b"database changed")
    assert first == fingerprint(tmp_path)
    (tmp_path / "app.py").write_text("a=2")
    assert first != fingerprint(tmp_path)


@pytest.mark.asyncio
async def test_confirmation_prevents_writes_and_stale_approval(manager):
    p = new_project(manager)
    run = await manager.new(p["id"], "counter")
    await settled(manager)
    assert manager.store.run(run["id"])["status"] == "waiting_confirmation"
    assert not list(Path(p["path"]).iterdir())
    with pytest.raises(ValueError, match="已更新"):
        await manager.decide(run["id"], DecisionInput(kind="confirm", expected_revision=999, decision_id="stale"))
    assert manager.agents.calls == ["planner"]


@pytest.mark.asyncio
async def test_happy_path_and_idempotent_confirm(manager):
    p, r = await approved(manager)
    assert r["status"] == "completed", r
    assert (Path(p["path"]) / "index.html").exists()
    assert manager.agents.calls == ["planner", "coder", "reviewer"]
    assert r["state"]["repair_count"] == 0
    await manager.decide(r["id"], DecisionInput(kind="confirm", expected_revision=1, decision_id="confirm"))
    assert manager.agents.calls.count("coder") == 1


@pytest.mark.asyncio
async def test_three_repairs_then_explicit_continue(manager):
    manager.agents.fail_checks = True
    _, r = await approved(manager)
    assert r["status"] == "waiting_repair", r
    assert r["state"]["repair_count"] == 3
    assert manager.agents.calls.count("coder") == 4
    manager.agents.fail_checks = False
    await manager.decide(r["id"], DecisionInput(kind="continue", expected_revision=1, decision_id="continue"))
    await settled(manager)
    current = manager.store.run(r["id"])
    assert current["status"] == "completed"
    assert current["state"]["batch_id"] == 2
    assert current["state"]["total_repairs"] == 4


@pytest.mark.asyncio
async def test_startup_failures_also_consume_repairs(manager):
    manager.processes.fail_start = True
    _, r = await approved(manager)
    assert r["status"] == "waiting_repair", r
    assert manager.agents.calls.count("coder") == 4
    assert r["state"]["repair_count"] == 3


@pytest.mark.asyncio
async def test_retry_cannot_reset_exhausted_budget(manager):
    manager.agents.fail_checks = True
    _, r = await approved(manager)
    await manager.stop(r["id"])
    retried = await manager.retry(r["id"])
    await settled(manager)
    assert manager.store.run(retried["id"])["status"] == "waiting_repair"
    assert manager.agents.calls.count("coder") == 4


@pytest.mark.asyncio
async def test_plan_failure_retry_does_not_skip_confirmation(manager):
    manager.agents.fail_plan = True
    p = new_project(manager)
    r = await manager.new(p["id"], "counter")
    await settled(manager)
    assert manager.store.run(r["id"])["status"] == "failed"
    retry = await manager.retry(r["id"])
    await settled(manager)
    assert manager.store.run(retry["id"])["status"] == "waiting_confirmation"
    assert "coder" not in manager.agents.calls


@pytest.mark.asyncio
async def test_pause_can_be_resumed_by_new_manager(manager):
    p = new_project(manager)
    r = await manager.new(p["id"], "counter")
    await settled(manager)
    rebuilt = Manager(manager.store, manager.settings, manager.checkpointer, manager.agents)
    rebuilt.processes = FakeProcesses()
    await rebuilt.decide(r["id"], DecisionInput(kind="confirm", expected_revision=1, decision_id="new-process"))
    await settled(rebuilt)
    assert rebuilt.store.run(r["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_single_active_execution_and_stop(manager):
    manager.agents.block = True
    p = new_project(manager)
    r = await manager.new(p["id"], "counter")
    await settled(manager)
    await manager.decide(r["id"], DecisionInput(kind="confirm", expected_revision=1, decision_id="start"))
    await asyncio.sleep(.05)
    other = new_project(manager, "other")
    with pytest.raises(BusyError):
        await manager.new(other["id"], "another")
    stopped = await manager.stop(r["id"])
    assert stopped["status"] == "stopped"
    assert manager.active_id is None


@pytest.mark.asyncio
async def test_build_failure_consumes_budget_but_dependency_failure_does_not(manager):
    original = manager.agents.invoke
    kind = "build"
    async def invoke(role, state, ctx):
        value = await original(role, state, ctx)
        if role == "coder":
            value = {**value, "setup_commands": [{"program": "npm", "args": ["run", "build"], "kind": kind}]}
        return value
    manager.agents.invoke = invoke
    _, run = await approved(manager)
    assert run["status"] == "waiting_repair"
    assert run["state"]["repair_count"] == 3
    await manager.stop(run["id"])
    kind = "dependency"
    project = new_project(manager, "dependency")
    run = await manager.new(project["id"], "counter")
    await settled(manager)
    await manager.decide(run["id"], DecisionInput(kind="confirm", expected_revision=1, decision_id="deps"))
    await settled(manager)
    run = manager.store.run(run["id"])
    assert run["status"] == "failed"
    assert run["state"]["repair_count"] == 0


@pytest.mark.asyncio
async def test_api_sources_evidence_and_stale_report(tmp_path):
    import httpx
    from backend.app.main import create_app
    app = create_app(Settings(root=tmp_path), FakeAgents())
    async with app.router.lifespan_context(app):
        manager = app.state.manager
        manager.processes = FakeProcesses()
        root = tmp_path / "generated"
        root.mkdir()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
            response = await client.post("/api/projects", json={"path": str(root), "name": "demo"})
            assert response.status_code == 201
            project = response.json()
            assert (await client.post("/api/projects", json={"path": str(root), "name": "again"})).status_code == 409
            run = (await client.post(f"/api/projects/{project['id']}/runs", json={"request": "counter"})).json()
            await settled(manager)
            response = await client.post(f"/api/runs/{run['id']}/decision", json={"kind": "confirm", "expected_revision": 1, "decision_id": "api"})
            assert response.status_code == 202
            await settled(manager)
            current = (await client.get(f"/api/runs/{run['id']}")).json()
            assert current["status"] == "completed" and current["report_current"]
            evidence = (await client.get(f"/api/runs/{run['id']}/evidence")).json()
            assert evidence[0]["assertion"]
            assert (await client.get(f"/api/projects/{project['id']}/files/content", params={"path": "../app.db"})).status_code == 400
            (root / "index.html").write_text("changed", encoding="utf-8")
            assert not (await client.get(f"/api/runs/{run['id']}")).json()["report_current"]
            response = await client.get(f"/api/runs/{run['id']}/events")
            assert "run.finished" in response.text and "event: closed" in response.text
            response = await client.post("/api/projects", headers={"Origin": "https://unrelated.example"}, json={"path": str(root)})
            assert response.status_code == 403


@pytest.mark.asyncio
async def test_coder_command_schema_and_real_subprocess(manager):
    from backend.app.tools import ToolContext, Processes
    ctx = ToolContext(manager.settings.root, "command-test", manager.settings,
                      Processes(manager.store, manager.settings), lambda *args: None)
    command = next(t for t in ctx.tools("coder") if t.name == "run_command")
    schema = command.args_schema.model_json_schema()
    assert "arguments" in schema["properties"] and "v__args" not in schema["properties"]
    result = await command.ainvoke({"program": "python", "arguments": ["-c", "print(17+25)"], "cwd": "."})
    assert result["passed"] and result["details"]["exit_code"] == 0
    assert result["details"]["output"].strip() == "42"
