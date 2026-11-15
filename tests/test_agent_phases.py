"""Phased work: the loop's context budget / end_turn / auto_allow, the PhaseRunner, and /phase in the TUI."""

import asyncio
import json

import pytest

from tai.agent.loop import Agent, AgentConfig, Budget, Decision, Notice, ToolEnd, Transcript, TurnEnd
from tai.agent.phases import STATE, PhaseRunner, Work, slug
from tai.agent.tools import ToolContext
from tai.agent.tui import AgentApp, AppInfo, PermissionScreen, PhaseGateScreen


def text_reply(*parts):
    return [{"choices": [{"delta": {"content": p}}]} for p in parts]


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
        yield from self.replies.pop(0)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    return tmp_path.resolve()


def make(project, *replies, window=1000):
    asked = []

    def approve(tool, args, preview):
        asked.append(tool.name)
        return Decision.ALLOW

    cfg = AgentConfig(model_id="m", system="SYS", ctx=ToolContext(root=project), context_window=window)
    return Agent(FakeClient(*replies), cfg, approve), asked


# -- loop --------------------------------------------------------------------
def test_budget_asks_the_model_to_wrap_up_once(project):
    agent, _ = make(project, tool_reply("read", {"path": "a.txt"}, tokens=850),
                    tool_reply("read", {"path": "a.txt"}, "c2", tokens=870), text_reply("ok"))
    agent.budget = Budget("wrap up at {pct}%")
    events = list(agent.run("go"))
    assert [e.text for e in events if isinstance(e, Notice)] == ["Context 85% full — asked the model to wrap up"]
    assert [m["content"] for m in agent.messages if m["role"] == "user"] == ["go", "wrap up at 85%"]
    assert events[-1] == TurnEnd("done")


def test_budget_stops_the_turn_when_nearly_full(project):
    agent, _ = make(project, tool_reply("read", {"path": "a.txt"}, tokens=960), text_reply("never sent"))
    agent.budget = Budget("wrap")
    events = list(agent.run("go"))
    assert events[-1] == TurnEnd("context_full")
    assert len(agent.client.requests) == 1


def test_no_budget_means_no_checks(project):
    agent, _ = make(project, tool_reply("read", {"path": "a.txt"}, tokens=990), text_reply("ok"))
    assert list(agent.run("go"))[-1] == TurnEnd("done")


def test_auto_allow_skips_the_question(project):
    agent, asked = make(project, tool_reply("write", {"path": "b.txt", "content": "x"}), text_reply("ok"))
    agent.auto_allow = lambda tool, args: args["path"] == "b.txt"
    list(agent.run("go"))
    assert asked == [] and (project / "b.txt").read_text() == "x"


def test_step_limit_ends_the_turn_but_not_in_phased_work(project):
    replies = [tool_reply("read", {"path": "a.txt"}, f"c{i}") for i in range(3)]
    agent, _ = make(project, *replies, text_reply("ok"))
    agent.config.max_steps = 2
    assert list(agent.run("go"))[-1] == TurnEnd("max_steps")
    agent, _ = make(project, *replies, text_reply("ok"))
    agent.config.max_steps, agent.budget = 2, Budget("wrap")
    assert list(agent.run("go"))[-1] == TurnEnd("done")


# -- runner ------------------------------------------------------------------
def test_start_creates_the_work_folder(project):
    agent, _ = make(project)
    runner = PhaseRunner(agent, project)
    work = runner.start("Add shell completion!")
    assert work.folder.parent == project / ".tai" / "work" and work.folder.name.endswith("add-shell-completion")
    assert all((work.folder / d).is_dir() for d in ("notes", "tools", "tests"))
    assert [p.name for p in Work.load(work.folder).phases] == ["explore", "plan"]
    assert "phase_done" in agent.config.tools and "# Phased work" in agent.config.system
    assert runner.start("Add shell completion!").folder.name.endswith("-2")  # never reuses a folder


def test_the_prompt_spells_out_the_work_folder_paths(project):
    """Tools resolve paths against the project: a bare notes/explore.md landed in the project root."""
    agent, _ = make(project)
    runner = PhaseRunner(agent, project)
    rel = runner.rel(runner.start("Add shell completion").folder)
    assert f"{rel}/plan.md" in runner.prompt() and f"{rel}/notes/" in runner.prompt()


def test_phase_done_ends_the_turn_and_the_next_phase_gets_the_handoff(project):
    agent, asked = make(project,
                        tool_reply("write", {"path": "PLACEHOLDER", "content": "# notes"}),
                        tool_reply("phase_done", {"handoff": "Found X in a.txt", "files": ["notes/explore.md"]}, "c2"),
                        text_reply("Planning."))
    runner = PhaseRunner(agent, project)
    work = runner.start("goal")
    notes = runner.rel(work.folder / "notes" / "explore.md")
    agent.client.replies[0] = tool_reply("write", {"path": notes, "content": "# notes"})

    events = list(agent.run(runner.begin()))
    assert events[-1] == TurnEnd("phase_done") and runner.finished
    assert asked == []  # writing in the work folder doesn't ask
    assert Work.load(work.folder).phases[0].status == "done"
    assert any(isinstance(e, ToolEnd) and "Next: plan" in e.result for e in events)

    prompt = runner.begin()
    assert agent.messages == [] and not runner.finished
    assert "Phase 2/2: plan" in prompt and "Found X in a.txt" in prompt and "notes/explore.md" in prompt


def test_plan_phase_must_define_the_rest(project):
    agent, _ = make(project)
    runner = PhaseRunner(agent, project)
    work = runner.start("goal")
    work.phases[0].status = "done"
    done = agent.config.tools["phase_done"]
    ctx = agent.config.ctx
    with pytest.raises(Exception, match="next_phases"):
        done.run({"handoff": "plan written"}, ctx)
    with pytest.raises(Exception, match="review"):  # an empty plan ended research-rail with nothing delivered
        done.run({"handoff": "plan written", "next_phases": []}, ctx)
    done.run({"handoff": "plan written", "next_phases": [{"name": "tests", "goal": "write failing tests"},
                                                          {"name": "build", "goal": "make them pass"}]}, ctx)
    assert [p.name for p in Work.load(work.folder).phases] == ["explore", "plan", "tests", "build"]
    assert runner.label() == "phase 3/4 · tests"


def test_writes_outside_the_work_folder_still_ask(project):
    agent, asked = make(project, tool_reply("write", {"path": "src.py", "content": "x"}), text_reply("ok"))
    runner = PhaseRunner(agent, project)
    runner.start("goal")
    list(agent.run(runner.begin()))
    assert asked == ["write"]


def test_resume_picks_the_latest_and_stop_restores(project):
    agent, _ = make(project)
    runner = PhaseRunner(agent, project)
    runner.start("first")
    second = runner.start("second").folder
    runner.stop()
    assert agent.config.system == "SYS" and "phase_done" not in agent.config.tools and agent.budget is None
    assert runner.resume().folder == second and runner.active


def test_slug():
    assert slug("Fix the  TUI's /phase bug!") == "fix-the-tui-s-phase-bug"
    assert slug("???") == "task"
    assert slug("add shell completion for bash and zsh, installed with tai completion") == \
        "add-shell-completion-for-bash-and-zsh"  # cut between words, not inside one
    assert slug("x" * 50) == "x" * 40
    assert slug("a" * 40 + " b") == "a" * 40


# -- TUI ---------------------------------------------------------------------
async def wait_until(pilot, cond, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await pilot.pause(0.02)
    raise AssertionError("timed out waiting for the UI")


def tui(project, *replies, **info):
    """An AgentApp on a fake client; returns (app, client)."""
    client = FakeClient(*replies)
    cfg = AgentConfig(model_id="m", system="SYS", ctx=ToolContext(root=project))
    agent = Agent(client, cfg, lambda *a: Decision.DENY, Transcript([]))
    return AgentApp(agent, AppInfo("tai agent · test", **info)), client


def test_phase_flow_in_the_tui(project):
    contexts = []

    async def main():
        app, client = tui(project, tool_reply("phase_done", {"handoff": "explored"}), text_reply("Which option?"),
                          on_new_context=contexts.append)
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("/phase start add a flag")
            await wait_until(pilot, lambda: isinstance(app.screen, PhaseGateScreen))
            assert "phase 2/2 · plan" in app._title()
            await pilot.press("y")
            await wait_until(pilot, lambda: not app.busy and len(client.requests) == 2)
            first_user = client.requests[1]["messages"][1]["content"]
            assert "Phase 2/2: plan" in first_user and "explored" in first_user
            assert len(client.requests[1]["messages"]) == 2  # fresh context: system + phase prompt
    asyncio.run(main())
    assert [c.split(" — ")[0] for c in contexts] == ["phase 1/2 · explore", "phase 2/2 · plan"]
    assert (next((project / ".tai" / "work").iterdir()) / STATE).is_file()


# -- breaking a task down: start_phases and --phase ----------------------------
def test_start_phases_is_offered_only_outside_phased_work(project):
    agent, _ = make(project)
    runner = PhaseRunner(agent, project)
    assert "start_phases" in agent.config.tools
    runner.start("goal")
    assert "start_phases" not in agent.config.tools
    runner.stop()
    assert "start_phases" in agent.config.tools


def test_start_phases_asks_and_ends_the_turn(project):
    args = {"goal": "add completion", "reason": "touches many files", "background": "user wants zsh"}
    agent, asked = make(project, tool_reply("start_phases", args))
    runner = PhaseRunner(agent, project)
    events = list(agent.run("add shell completion for every command"))
    assert asked == ["start_phases"] and events[-1] == TurnEnd("start_phases")
    assert runner.take_request() == ("add completion", "user wants zsh") and runner.requested is None


def test_denied_start_phases_requests_nothing(project):
    cfg = AgentConfig(model_id="m", system="SYS", ctx=ToolContext(root=project))
    agent = Agent(FakeClient(tool_reply("start_phases", {"goal": "g", "reason": "r"}), text_reply("ok")), cfg,
                  lambda *a: Decision.DENY)
    runner = PhaseRunner(agent, project)
    assert list(agent.run("big task"))[-1] == TurnEnd("done") and runner.requested is None


def test_background_reaches_every_phase(project):
    agent, _ = make(project)
    runner = PhaseRunner(agent, project)
    work = runner.start("goal", "user wants zsh only")
    assert "user wants zsh only" in runner.begin()
    assert Work.load(work.folder).background == "user wants zsh only"


def test_agent_switches_to_phases_in_the_tui(project):
    contexts = []

    async def main():
        args = {"goal": "add completion", "reason": "many files", "background": "zsh first"}
        app, client = tui(project, tool_reply("start_phases", args), text_reply("Exploring."),
                          on_new_context=contexts.append)
        async with app.run_test(size=(100, 30)) as pilot:
            app._submit("add shell completion")
            await wait_until(pilot, lambda: isinstance(app.screen, PermissionScreen))
            assert app.screen.preview.title == "Switch to phased work?"
            await pilot.press("y")
            await wait_until(pilot, lambda: not app.busy and len(client.requests) == 2)
            first = client.requests[1]["messages"]
            assert len(first) == 2 and "Phase 1/2: explore" in first[1]["content"]
            assert "zsh first" in first[1]["content"] and app.phases.active
    asyncio.run(main())
    assert contexts[0].startswith("phase 1/2 · explore — add completion")


def test_phase_goal_starts_phased_work_on_launch(project):
    async def main():
        app, client = tui(project, text_reply("Exploring."), phase_goal="add a flag")
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_until(pilot, lambda: not app.busy and client.requests)
            assert app.phases.active and app.phases.work.goal == "add a flag"
            assert "Phase 1/2: explore" in client.requests[0]["messages"][1]["content"]
    asyncio.run(main())


def test_phase_flag_needs_a_new_task():
    from click.testing import CliRunner

    import tai.cli as cli_mod
    r = CliRunner().invoke(cli_mod.cli, ["agent", "--phase"])
    assert r.exit_code == 2 and "--phase needs a TASK" in r.output
    r = CliRunner().invoke(cli_mod.cli, ["agent", "--phase", "-c", "goal"])
    assert r.exit_code == 2 and "/phase resume" in r.output
