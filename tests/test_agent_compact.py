"""Context compaction: where the kept tail starts, Agent.compact, automatic compaction, /compact in the TUI."""

import asyncio
import json

import pytest

from tai.agent import compact
from tai.agent.command import compact_at
from tai.agent.compact import Compactor
from tai.agent.loop import (Agent, AgentConfig, Budget, Compacted, Decision, Notice, SummaryProgress, TextDelta,
                            Transcript, TurnEnd)
from tai.agent.tools import ToolContext
from tai.agent.tui import AgentApp, AppInfo
from tai.client import TiinyError
from tai.db import Store


def text_reply(*parts, tokens=0):
    chunks = [{"choices": [{"delta": {"content": p}}]} for p in parts]
    if tokens:
        chunks.append({"choices": [], "usage": {"prompt_tokens": tokens - 10, "completion_tokens": 10}})
    return chunks


def tool_reply(name, args, call_id="c1", tokens=0):
    call = {"index": 0, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}
    chunks = [{"choices": [{"delta": {"tool_calls": [call]}}]}]
    if tokens:
        chunks.append({"choices": [], "usage": {"prompt_tokens": tokens - 10, "completion_tokens": 10}})
    return chunks


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat_stream(self, model, messages, **kw):
        self.requests.append({"messages": [dict(m) for m in messages], **kw})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield from reply


HISTORY = [
    {"role": "user", "content": "fix the bug"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.txt"}'}}]},
    {"role": "tool", "tool_call_id": "t1", "content": "hello\n"},
    {"role": "assistant", "content": "Found it."},
]


@pytest.fixture
def project(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    return tmp_path.resolve()


def make(project, *replies, messages=None, window=1000, at=85):
    replaced = []
    cfg = AgentConfig(model_id="m", system="SYS", ctx=ToolContext(root=project), context_window=window)
    transcript = Transcript([dict(m) for m in (messages or [])], on_replace=replaced.append)
    agent = Agent(FakeClient(*replies), cfg, lambda *a: Decision.ALLOW, transcript)
    Compactor(agent, at)
    return agent, replaced


# -- split_point -------------------------------------------------------------
def test_split_never_separates_a_tool_result_from_its_call():
    big = [{"role": "user", "content": "x" * 50}, *HISTORY]
    for limit in range(0, 200, 5):
        cut = compact.split_point(big, limit)
        assert cut == len(big) or big[cut]["role"] in ("user", "assistant")


def test_split_keeps_what_fits():
    assert compact.split_point(HISTORY, 10_000) == 0
    assert compact.split_point(HISTORY, 9) == 3  # only "Found it."
    assert compact.split_point(HISTORY, 0) == len(HISTORY)


def test_keep_chars_follows_small_windows():
    assert compact.keep_chars(0) == compact.KEEP_CHARS
    assert compact.keep_chars(8000) == 3200


# -- Agent.compact -----------------------------------------------------------
def test_compact_replaces_the_conversation_with_the_summary(project):
    agent, replaced = make(project, text_reply("## Task\n", "fix the bug"), messages=HISTORY)
    events = list(agent.compactor.run("the bug"))
    req = agent.client.requests[0]
    assert req["messages"][0]["content"] == "SYS" and req["messages"][1:5] == HISTORY  # same prefix: cache reuse
    assert req["messages"][-1]["content"].endswith("Pay special attention to: the bug") and req["tools"]
    assert not any(isinstance(e, TextDelta) for e in events)  # the summary isn't a chat message
    assert [e.chars for e in events if isinstance(e, SummaryProgress)] == [8, 19]
    done = events[-1]
    assert isinstance(done, Compacted) and done.summary == "## Task\nfix the bug" and done.before == 4
    assert compact.is_summary(agent.messages[0]) and agent.messages[1:] == HISTORY[4 - done.kept:]
    assert replaced == [agent.messages]


def test_failed_compaction_keeps_the_conversation(project):
    for reply in (TiinyError("504 timeout"), text_reply("")):
        agent, replaced = make(project, reply, messages=HISTORY)
        events = list(agent.compactor.run())
        assert agent.messages == HISTORY and replaced == []
        assert "stays as it was" in events[-1].text


def test_nothing_to_compact(project):
    agent, _ = make(project)
    assert [e.text for e in agent.compactor.run()] == ["Nothing to compact yet"]


# -- automatic ---------------------------------------------------------------
def test_compacts_before_a_turn_when_the_context_is_full(project):
    agent, _ = make(project, text_reply("summary"), text_reply("ok"), messages=HISTORY)
    agent.tokens = 900
    events = list(agent.run("next"))
    assert any(isinstance(e, Compacted) for e in events) and events[-1] == TurnEnd("done")
    turn = agent.client.requests[1]["messages"]
    assert compact.is_summary(turn[1]) and turn[-1] == {"role": "user", "content": "next"}


def test_compacts_mid_turn_and_continues(project):
    agent, _ = make(project, tool_reply("read", {"path": "a.txt"}, tokens=900), text_reply("summary"),
                    text_reply("done"))
    events = list(agent.run("go"))
    assert any(isinstance(e, Compacted) for e in events) and events[-1] == TurnEnd("done")
    assert compact.is_summary(agent.client.requests[2]["messages"][1])


def test_a_failed_automatic_compaction_is_not_retried_every_step(project):
    agent, _ = make(project, tool_reply("read", {"path": "a.txt"}, tokens=900), TiinyError("boom"),
                    tool_reply("read", {"path": "a.txt"}, "c2", tokens=920), text_reply("done"))
    events = list(agent.run("go"))
    assert events[-1] == TurnEnd("done") and len(agent.client.requests) == 4
    assert sum("Compaction failed" in e.text for e in events if isinstance(e, Notice)) == 1


def test_no_automatic_compaction_when_off_or_in_phases(project):
    for at, budget in ((0, None), (85, Budget("wrap"))):
        agent, _ = make(project, text_reply("ok"), messages=HISTORY, at=at)
        agent.budget, agent.tokens = budget, 900
        assert not any(isinstance(e, Compacted) for e in agent.run("next"))


def test_compact_at_setting(tmp_path):
    class App:
        store = Store(tmp_path / "t.db")
    assert compact_at(App) == compact.DEFAULT_AT
    App.store.set_setting("agent.compact", "off")
    assert compact_at(App) == 0
    App.store.set_setting("agent.compact", "70")
    assert compact_at(App) == 70


# -- TUI ---------------------------------------------------------------------
async def wait_until(pilot, cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await pilot.pause(0.02)
    raise AssertionError("timed out waiting for the UI")


def test_compact_command_in_the_tui(project):
    async def main():
        agent, _ = make(project, text_reply("the summary"), messages=HISTORY)
        app = AgentApp(agent, AppInfo("tai agent · test"))
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("/compact")
            await wait_until(pilot, lambda: not app.busy and compact.is_summary(agent.messages[0]))
            notes = [str(w.content) for w in app.query(".notice")]
            assert any("Compacted 4 messages" in n for n in notes)
    asyncio.run(main())


def test_recheck_setting(tmp_path):
    from tai.agent.command import recheck

    class App:
        store = Store(tmp_path / "t.db")
    assert recheck(App)
    App.store.set_setting("agent.recheck", "off")
    assert not recheck(App)
