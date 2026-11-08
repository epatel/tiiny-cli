"""Agent loop against a fake streaming client: tool calls, permissions, interrupts."""

import json

import pytest

from tai.agent.loop import (DENIED, Agent, AgentConfig, Decision, Notice, ReasoningDelta, TextDelta, ToolEnd,
                            ToolStart, Transcript, TurnEnd)
from tai.agent.tools import ToolContext
from tai.client import TiinyError


def text_reply(*parts):
    return [{"choices": [{"delta": {"content": p}}]} for p in parts]


def tool_reply(name, args, call_id="c1", split=True):
    """A tool call streamed the way the device does: id+name first, arguments in pieces."""
    raw = json.dumps(args) if isinstance(args, dict) else args
    pieces = [raw[:5], raw[5:]] if split else [raw]
    first = {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}
    return [{"choices": [{"delta": {"tool_calls": [first]}}]}] + [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": p}}]}}]} for p in pieces]


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat_stream(self, model, messages, **kw):
        self.requests.append({"model": model, "messages": [dict(m) for m in messages], **kw})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        yield from reply


@pytest.fixture
def project(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    return tmp_path.resolve()


def make(project, *replies, approve=None, transcript=None):
    client = FakeClient(*replies)
    asked = []

    def default_approve(tool, args, preview):
        asked.append((tool.name, preview.title))
        return Decision.ALLOW

    cfg = AgentConfig(model_id="m", system="SYS", ctx=ToolContext(root=project))
    agent = Agent(client, cfg, approve or default_approve, transcript)
    return agent, client, asked


def test_plain_answer(project):
    agent, client, _ = make(project, text_reply("Hel", "lo"))
    events = list(agent.run("hi"))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    assert events[-1] == TurnEnd("done")
    assert agent.messages[-1] == {"role": "assistant", "content": "Hello"}
    req = client.requests[0]
    assert req["messages"][0] == {"role": "system", "content": "SYS"}
    assert {t["function"]["name"] for t in req["tools"]} >= {"read", "edit", "bash", "grep"}


def test_tool_call_roundtrip_without_permission(project):
    agent, client, asked = make(project, tool_reply("read", {"path": "a.txt"}), text_reply("It says hello."))
    events = list(agent.run("what is in a.txt?"))
    start = next(e for e in events if isinstance(e, ToolStart))
    end = next(e for e in events if isinstance(e, ToolEnd))
    assert start.label == "read a.txt" and end.ok and "hello" in end.result
    assert asked == []  # read never asks
    roles = [m["role"] for m in client.requests[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert client.requests[1]["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'
    assert client.requests[1]["messages"][3]["tool_call_id"] == "c1"


def test_edit_asks_and_applies(project):
    edit = {"path": "a.txt", "old_string": "hello", "new_string": "bye"}
    agent, _, asked = make(project, tool_reply("edit", edit), text_reply("done"))
    list(agent.run("change it"))
    assert asked == [("edit", "Edit a.txt")]
    assert (project / "a.txt").read_text() == "bye\n"


def test_denied_call_is_reported_to_model_and_not_run(project):
    agent, client, _ = make(project, tool_reply("bash", {"command": "touch x"}), text_reply("ok"),
                            approve=lambda t, a, p: Decision.DENY)
    events = list(agent.run("make x"))
    end = next(e for e in events if isinstance(e, ToolEnd))
    assert end.denied and not end.ok and end.result == DENIED
    assert not (project / "x").exists()


def test_always_grants_the_group_for_the_session(project):
    calls = []

    def approve(tool, args, preview):
        calls.append(tool.name)
        return Decision.ALWAYS

    agent, _, _ = make(project, tool_reply("bash", {"command": "true"}), tool_reply("bash", {"command": "true"}, "c2"),
                       text_reply("ok"), approve=approve)
    list(agent.run("go"))
    assert calls == ["bash"]  # second bash ran without asking


def test_always_never_covers_files_outside_the_project(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("other") / "o.txt"
    calls = []

    def approve(tool, args, preview):
        calls.append(preview.outside)
        return Decision.ALWAYS

    agent, _, _ = make(project, tool_reply("write", {"path": str(outside), "content": "x"}),
                       tool_reply("write", {"path": str(outside), "content": "y"}, "c2"),
                       text_reply("ok"), approve=approve)
    list(agent.run("go"))
    assert calls == [True, True]


def test_bad_json_unknown_tool_and_tool_errors_go_back_to_model(project):
    agent, _, _ = make(project, tool_reply("read", "{not json", split=False), tool_reply("fly", {}),
                       tool_reply("read", {"path": "missing.txt"}), text_reply("sorry"))
    results = [e.result for e in agent.run("x") if isinstance(e, ToolEnd)]
    assert results[0] == "Error: tool arguments were not valid JSON"
    assert results[1].startswith("Error: unknown tool 'fly'")
    assert results[2].startswith("Error: File not found")


def test_cancel_mid_stream_keeps_history_consistent(project):
    agent, client, _ = make(project, text_reply("a", "b", "c"))
    events = agent.run("hi")
    assert next(events) == TextDelta("a")
    agent.cancel()
    rest = list(events)
    assert rest[-1] == TurnEnd("interrupted")
    assert agent.messages[-1] == {"role": "assistant", "content": "a"}


def test_cancel_before_tools_answers_every_call(project):
    agent, _, _ = make(project, tool_reply("bash", {"command": "touch y"}))
    events = agent.run("go")
    for e in events:
        if isinstance(e, ToolStart):
            agent.cancel()
    assert not (project / "y").exists()
    assert agent.messages[-1]["role"] == "tool" and "Interrupted" in agent.messages[-1]["content"]


def test_max_steps(project):
    agent, _, _ = make(project, *[tool_reply("ls", {}, f"c{i}") for i in range(3)])
    agent.config.max_steps = 3
    assert list(agent.run("loop"))[-1] == TurnEnd("max_steps")


def test_device_error_ends_turn(project):
    agent, _, _ = make(project, TiinyError("HTTP 404: model not loaded"))
    assert list(agent.run("hi"))[-1] == TurnEnd("error", "HTTP 404: model not loaded")


def test_reasoning_and_transcript_persistence(project):
    saved = []
    reply = [{"choices": [{"delta": {"reasoning_content": "think"}}]}] + text_reply("ok")
    agent, _, _ = make(project, reply, transcript=Transcript(on_message=saved.append))
    events = list(agent.run("hi"))
    assert ReasoningDelta("think") in events
    assert [m["role"] for m in saved] == ["user", "assistant"]


def test_extra_tools_from_config_are_offered_and_permission_checked(project):
    from tai.agent.tools import TOOLS, Preview, Tool
    calls = []
    remote = Tool("mcp__srv__write", "[MCP srv] write", {}, lambda a, c: calls.append(a) or "written",
                  lambda a, c: Preview("MCP srv · write", "{}", "json"), grant="mcp:srv",
                  schema={"type": "object", "properties": {"x": {"type": "integer"}}})
    asked = []
    agent, client, _ = make(project, tool_reply("mcp__srv__write", {"x": 1}), text_reply("done"),
                            approve=lambda t, a, p: asked.append(p.title) or Decision.ALLOW)
    agent.config.tools = {**TOOLS, remote.name: remote}
    list(agent.run("go"))
    assert asked == ["MCP srv · write"] and calls == [{"x": 1}]
    offered = {t["function"]["name"]: t for t in client.requests[0]["tools"]}
    assert offered["mcp__srv__write"]["function"]["parameters"]["properties"] == {"x": {"type": "integer"}}


MISSING = TiinyError('HTTP 404 POST /v1/chat/completions: "m" is not loaded.')


def test_missing_model_is_reloaded_and_the_step_retried(project):
    agent, client, _ = make(project, MISSING, text_reply("back"))
    reloads = []
    agent.reload_model = lambda: reloads.append(True)
    events = list(agent.run("hi"))
    notices = [e.text for e in events if isinstance(e, Notice)]
    assert reloads == [True] and len(notices) == 2 and "no longer loaded" in notices[0]
    assert events[-1] == TurnEnd("done") and agent.messages[-1] == {"role": "assistant", "content": "back"}
    assert len(client.requests) == 2


def test_failed_reload_ends_the_turn_with_its_error(project):
    agent, _, _ = make(project, MISSING)

    def fail():
        raise TiinyError("Reload cancelled: m is not loaded.")

    agent.reload_model = fail
    assert list(agent.run("hi"))[-1] == TurnEnd("error", "Reload cancelled: m is not loaded.")


def test_without_reload_hook_or_for_other_errors_no_reload(project):
    agent, _, _ = make(project, MISSING)
    assert list(agent.run("hi"))[-1].reason == "error"  # no hook
    agent, _, _ = make(project, TiinyError("HTTP 500: boom"))
    agent.reload_model = lambda: pytest.fail("should not reload")
    assert list(agent.run("hi"))[-1] == TurnEnd("error", "HTTP 500: boom")
