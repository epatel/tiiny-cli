"""AgentApp driven headlessly with textual's pilot, against a fake streaming client."""

import asyncio
import json

from rich.markdown import Markdown
from rich.text import Text
from textual.widgets import Static

from tai.agent.loop import Agent, AgentConfig, Decision, Transcript
from tai.agent.tools import ToolContext
from tai.agent.tui import AgentApp, AppInfo, PermissionScreen


def text_reply(*parts):
    return [{"choices": [{"delta": {"content": p}}]} for p in parts]


def tool_reply(name, args, call_id="c1"):
    call = {"index": 0, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}
    return [{"choices": [{"delta": {"tool_calls": [call]}}]}]


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)

    def chat_stream(self, model, messages, **kw):
        yield from self.replies.pop(0)


def make_app(root, *replies, messages=None, initial=None, on_clear=lambda: None):
    cfg = AgentConfig(model_id="m", system="SYS", ctx=ToolContext(root=root.resolve()))
    agent = Agent(FakeClient(*replies), cfg, lambda *a: Decision.DENY, Transcript(messages or []))
    return AgentApp(agent, AppInfo("tai agent · test", initial=initial, on_clear=on_clear))


def texts(app):
    out = []
    for w in app.query(Static):
        c = w.content
        out.append(c.plain if isinstance(c, Text) else c.markup if isinstance(c, Markdown) else str(c))
    return "\n".join(out)


async def wait_until(pilot, cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await pilot.pause(0.02)
    raise AssertionError("timed out waiting for the UI")


async def send(pilot, text):
    await pilot.click("#prompt")
    for ch in text:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")


def run(coro):
    asyncio.run(coro)


def test_plain_answer_streams_into_log(tmp_path):
    async def main():
        app = make_app(tmp_path, text_reply("Hello ", "**there**"))
        async with app.run_test(size=(100, 30)) as pilot:
            await send(pilot, "hi")
            await wait_until(pilot, lambda: not app.busy and "Hello" in texts(app))
            assert "› hi" in texts(app) and "Hello **there**" in texts(app)
            assert "ready" in texts(app)
    run(main())


def test_edit_is_approved_in_modal(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")

    async def main():
        app = make_app(tmp_path, tool_reply("edit", {"path": "a.txt", "old_string": "hello", "new_string": "bye"}),
                       text_reply("Changed it."))
        async with app.run_test(size=(100, 30)) as pilot:
            await send(pilot, "change")
            await wait_until(pilot, lambda: isinstance(app.screen, PermissionScreen))
            assert app.screen.preview.title == "Edit a.txt" and "+bye" in app.screen.preview.body
            await pilot.press("y")
            await wait_until(pilot, lambda: not app.busy)
            assert (tmp_path / "a.txt").read_text() == "bye\n"
            assert "⏺ edit a.txt" in texts(app) and "Changed it." in texts(app)
    run(main())


def test_denied_command_does_not_run(tmp_path):
    async def main():
        app = make_app(tmp_path, tool_reply("bash", {"command": "touch made"}), text_reply("OK, I won't."))
        async with app.run_test(size=(100, 30)) as pilot:
            await send(pilot, "make a file")
            await wait_until(pilot, lambda: isinstance(app.screen, PermissionScreen))
            await pilot.press("n")
            await wait_until(pilot, lambda: not app.busy)
            assert not (tmp_path / "made").exists()
            assert "⏺ bash touch made ✗ denied" in texts(app)
    run(main())


def test_always_skips_the_second_prompt(tmp_path):
    async def main():
        app = make_app(tmp_path, tool_reply("bash", {"command": "true"}), tool_reply("bash", {"command": "true"}, "c2"),
                       text_reply("done"))
        prompts = []
        async with app.run_test(size=(100, 30)) as pilot:
            await send(pilot, "go")
            await wait_until(pilot, lambda: isinstance(app.screen, PermissionScreen))
            prompts.append(app.screen.preview.title)
            await pilot.press("a")
            await wait_until(pilot, lambda: not app.busy)
            assert prompts == ["Run command"] and app.agent.granted == {"bash"}
    run(main())


def test_initial_task_resume_replay_and_clear(tmp_path):
    cleared = []
    history = [{"role": "user", "content": "earlier question"},
               {"role": "assistant", "content": None, "tool_calls": [
                   {"id": "c0", "type": "function", "function": {"name": "read", "arguments": '{"path": "x.py"}'}}]},
               {"role": "tool", "tool_call_id": "c0", "content": "..."},
               {"role": "assistant", "content": "earlier answer"}]

    async def main():
        app = make_app(tmp_path, text_reply("new answer"), messages=history, initial="next task",
                       on_clear=lambda: cleared.append(True))
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_until(pilot, lambda: not app.busy and "new answer" in texts(app))
            log = texts(app)
            assert "› earlier question" in log and "⏺ read x.py" in log and "earlier answer" in log
            assert "› next task" in log
            await send(pilot, "/clear")
            await pilot.pause()
            assert cleared == [True] and app.agent.messages == []
            assert "earlier answer" not in texts(app)
    run(main())


def test_help_and_busy_guard(tmp_path):
    async def main():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await send(pilot, "/help")
            await pilot.pause()
            assert "Esc interrupt" in texts(app)
    run(main())


def test_skills_mcp_listing_and_skill_shortcut(tmp_path):
    async def main():
        cfg = AgentConfig(model_id="m", system="SYS", ctx=ToolContext(root=tmp_path.resolve()))
        client = FakeClient(text_reply("ok"))
        agent = Agent(client, cfg, lambda *a: Decision.DENY, Transcript())
        info = AppInfo("t", skills=[("release", "Cut a release")], servers=["echo — 3 tools", "web — failed: 401"])
        app = AgentApp(agent, info)
        async with app.run_test(size=(100, 30)) as pilot:
            await send(pilot, "/skills")
            await send(pilot, "/mcp")
            await pilot.pause()
            log = texts(app)
            assert "**release** — Cut a release" in log and "web — failed: 401" in log
            await send(pilot, "/release v1.2")
            await wait_until(pilot, lambda: not app.busy and "ok" in texts(app))
            assert agent.messages[0] == {"role": "user", "content": "Use the `release` skill. v1.2"}
    run(main())


def test_json_preview_for_mcp_calls(tmp_path):
    from tai.agent.tools import Preview
    screen = PermissionScreen(Preview("MCP srv · write", '{"x": 1}', "json"))
    assert screen.preview.kind == "json"


async def type_text(pilot, text):
    for ch in text:
        await pilot.press({" ": "space", "\\": "backslash"}.get(ch, ch))


def test_multiline_input_newline_keys_and_send(tmp_path):
    from tai.agent.tui import PromptArea

    async def main():
        app = make_app(tmp_path, text_reply("got it"))
        async with app.run_test(size=(100, 30)) as pilot:
            area = app.query_one(PromptArea)
            await pilot.click("#prompt")
            await type_text(pilot, "first")
            await pilot.press("ctrl+j")
            await type_text(pilot, "second\\")
            await pilot.press("enter")  # trailing backslash: newline, not send
            await type_text(pilot, "third")
            await pilot.press("alt+enter")
            await type_text(pilot, "fourth")
            assert area.text == "first\nsecond\nthird\nfourth" and not app.busy
            await pilot.press("enter")
            await wait_until(pilot, lambda: not app.busy and "got it" in texts(app))
            assert app.agent.messages[0]["content"] == "first\nsecond\nthird\nfourth"
            assert area.text == ""
            assert "› first\nsecond\nthird\nfourth" in texts(app)
    run(main())


def test_pasted_newlines_are_kept_and_not_sent(tmp_path):
    from textual import events

    from tai.agent.tui import PromptArea

    async def main():
        app = make_app(tmp_path, text_reply("ok"))
        async with app.run_test(size=(100, 30)) as pilot:
            area = app.query_one(PromptArea)
            area.focus()
            app.post_message(events.Paste("line one\nline two\n"))  # as the terminal driver does
            await pilot.pause()
            assert area.text == "line one\nline two\n" and not app.busy
            await pilot.press("enter")
            await wait_until(pilot, lambda: not app.busy and "ok" in texts(app))
            assert app.agent.messages[0]["content"] == "line one\nline two"  # trimmed
    run(main())


def test_blank_enter_sends_nothing(tmp_path):
    from tai.agent.tui import PromptArea

    async def main():
        app = make_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click("#prompt")
            await pilot.press("ctrl+j", "enter")
            await pilot.pause()
            assert app.agent.messages == [] and app.query_one(PromptArea).text == ""
    run(main())


class DeviceClient(FakeClient):
    """FakeClient plus the device endpoints /tiiny uses."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.calls = []

    def models(self):
        return [{"id": "q/Coder", "display_name": "Coder", "type": "Text Generation", "npu_usage": 94,
                 "status": "running"}]

    def running(self):
        return {"instances": {"running": [{"model_id": "q/Coder", "status": "running"}]}}

    def shutdown(self):
        self.calls.append("shutdown")


def make_device_app(root):
    cfg = AgentConfig(model_id="q/Coder", system="SYS", ctx=ToolContext(root=root.resolve()))
    agent = Agent(DeviceClient(), cfg, lambda *a: Decision.DENY, Transcript())
    return AgentApp(agent, AppInfo("t"))


async def tiiny(pilot, app, line, answer=None):
    """Send `/tiiny <line>`, answer its confirmation with ``answer`` (y/n) if given, wait for it."""
    from tai.agent.tui import ConfirmScreen
    await send(pilot, f"/tiiny {line}")
    if answer:
        await wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        question = app.screen.question
        await pilot.press(answer)
    else:
        question = None
    await wait_until(pilot, lambda: not app.device_busy)
    return question


def test_tiiny_models_and_shutdown_confirmation(tmp_path):
    async def main():
        app = make_device_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await tiiny(pilot, app, "models")
            assert "| ● | Coder *(agent)* | chat | 94% |" in texts(app)
            question = await tiiny(pilot, app, "shutdown", answer="n")
            assert "Shut down the Tiiny?" in question
            assert app.agent.client.calls == [] and "Cancelled." in texts(app)
            await tiiny(pilot, app, "shutdown", answer="y")
            assert app.agent.client.calls == ["shutdown"] and "Shutting down the Tiiny." in texts(app)
    run(main())


def test_tiiny_errors_are_shown_not_raised(tmp_path):
    async def main():
        app = make_device_app(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await tiiny(pilot, app, "load llama")
            assert "**error:** No model matches 'llama'" in texts(app)
    run(main())
