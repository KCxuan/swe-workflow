"""Real LangChain loop with scripted model replies; no remote model requests."""
import asyncio

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import Field
import pytest

from backend.app.agents import Agents, AgentOutputError, OUTPUT_RETRIES
from backend.app.config import Settings
from backend.app.tools import ToolContext

CODING = {"summary": "计数器已写入", "changed_files": ["index.html"], "setup_commands": [], "checks": [],
          "services": [{"name": "web", "program": "python", "args": ["-m", "http.server", "12345"],
                        "port": 12345, "preview": True, "cwd": ".", "health_path": "/"}]}


class ScriptedModel(FakeMessagesListChatModel):
    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen_messages.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def ready():
    return AIMessage(content="", tool_calls=[{"name": "CodingResult", "args": CODING, "id": "output"}])


def setup(tmp_path, monkeypatch, responses, timeout=5, sleep=None):
    root = tmp_path / "project"
    root.mkdir()
    settings = Settings(root=tmp_path, agent_timeout=timeout)
    events = []
    ctx = ToolContext(root, "agent-test", settings, None, lambda kind, data: events.append((kind, data)))
    model = ScriptedModel(responses=responses, sleep=sleep)
    agent = Agents(settings)
    monkeypatch.setattr(agent, "model", lambda: model)
    return agent, ctx, model, events


@pytest.mark.asyncio
async def test_valid_result_does_not_retry(tmp_path, monkeypatch):
    agent, ctx, model, events = setup(tmp_path, monkeypatch, [ready()])
    assert await agent.invoke("coder", {"request": "counter"}, ctx) == CODING
    assert len(model.seen_messages) == 1
    assert not any(kind == "agent.retrying" for kind, _ in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
async def test_missing_output_continues_history_without_repeating_write(tmp_path, monkeypatch, stop_reason):
    write = AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "index.html", "content": "<button>+</button>"}, "id": "write-1"}])
    missing = AIMessage(content="实现已完成。", response_metadata={"stop_reason": stop_reason},
                        usage_metadata={"input_tokens": 500, "output_tokens": 12000, "total_tokens": 12500})
    agent, ctx, model, events = setup(tmp_path, monkeypatch, [write, missing, ready()])
    result = await agent.invoke("coder", {"request": "counter"}, ctx)
    assert result == CODING
    assert (ctx.root / "index.html").read_text() == "<button>+</button>"
    assert sum(kind == "file.written" for kind, _ in events) == 1
    assert len(model.seen_messages) == 3
    continued = model.seen_messages[-1]
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "write-1" for m in continued)
    assert any(m.content == missing.content for m in continued)
    diagnostic = next(data for kind, data in events if kind == "agent.output_invalid")
    assert diagnostic["stop_reason"] == stop_reason and diagnostic["output_tokens"] == 12000
    assert events[-1][1]["output_retries"] == 1


@pytest.mark.asyncio
async def test_repeated_missing_output_is_bounded_and_preserves_files(tmp_path, monkeypatch):
    agent, ctx, model, events = setup(tmp_path, monkeypatch, [AIMessage(content="普通文本，没有结果工具")])
    (ctx.root / "keep.txt").write_text("keep")
    with pytest.raises(AgentOutputError, match="自动补全未成功"):
        await agent.invoke("coder", {}, ctx)
    assert len(model.seen_messages) == OUTPUT_RETRIES + 1
    assert sum(kind == "agent.retrying" for kind, _ in events) == OUTPUT_RETRIES
    assert not any(kind == "agent.finished" for kind, _ in events)
    assert (ctx.root / "keep.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_bad_structured_tool_arguments_remain_validated(tmp_path, monkeypatch):
    malformed = AIMessage(content="", tool_calls=[{"name": "CodingResult", "args": {"summary": "incomplete"}, "id": "bad"}])
    agent, ctx, model, events = setup(tmp_path, monkeypatch, [malformed, ready()])
    assert await agent.invoke("coder", {}, ctx) == CODING
    assert len(model.seen_messages) == 2
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "bad" for m in model.seen_messages[-1])


@pytest.mark.asyncio
async def test_recovery_shares_original_timeout(tmp_path, monkeypatch):
    agent, ctx, model, events = setup(tmp_path, monkeypatch, [AIMessage(content="missing")], timeout=.16, sleep=.1)
    with pytest.raises(AgentOutputError, match="时限"):
        await agent.invoke("coder", {}, ctx)
    assert sum(kind == "agent.retrying" for kind, _ in events) <= 1


@pytest.mark.asyncio
async def test_cancellation_is_not_retried(tmp_path, monkeypatch):
    agent, ctx, model, events = setup(tmp_path, monkeypatch, [ready()], sleep=.2)
    task = asyncio.create_task(agent.invoke("coder", {}, ctx))
    await asyncio.sleep(.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(kind == "agent.retrying" for kind, _ in events)
