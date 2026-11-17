"""The loop's nudges: continue after an announced-but-not-done reply, a note in fix loops."""

import pytest

from tai.agent.loop import Notice, TurnEnd
from tai.agent.nudges import CONTINUE, FIX_LOOP, MAX_CONTINUES, Nudges, announces
from test_agent_loop import make, project, text_reply, tool_reply  # noqa: F401 (fixture)


@pytest.mark.parametrize("text, expected", [
    ("Let me start by exploring the project structure to understand the codebase.", True),
    ("Done. I'll now run the tests:", True),
    ("First, I need to read the tests", True),
    ("All 12 tests pass. The fallback to outer scopes is implemented.", False),
    ("The file says hello.", False),
    ("Let me know if you want anything else!", False),
    ("I should mention: the tests were already failing.", False),
])
def test_announces(text, expected):
    assert announces(text) is expected


def test_an_announcement_without_a_tool_call_gets_a_continue(project):
    agent, client, _ = make(project, text_reply("Let me start by exploring the project."),
                            tool_reply("read", {"path": "a.txt"}), text_reply("It says hello."))
    events = list(agent.run("what is in a.txt?"))
    assert any(isinstance(e, Notice) and "continue" in e.text for e in events)
    assert events[-1] == TurnEnd("done") and len(client.requests) == 3
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": CONTINUE}


def test_continues_are_limited_per_turn(project):
    replies = [text_reply("Let me look.") for _ in range(MAX_CONTINUES + 1)]
    agent, client, _ = make(project, *replies)
    assert list(agent.run("hi"))[-1] == TurnEnd("done")
    assert len(client.requests) == MAX_CONTINUES + 1


def test_nudges_off_ends_on_an_announcement(project):
    agent, client, _ = make(project, text_reply("Let me start."))
    agent.config.nudges = False
    list(agent.run("hi"))
    assert len(client.requests) == 1


def _bash(n):
    return {"command": "  python -m pytest  -q"}, f"exit code {n}\nFAILED test_x"


def test_fix_loop_note_after_repeated_failures_of_the_same_command():
    nudges = Nudges()
    nudges.new_turn("Make IDs never reused.")
    args, failing = _bash(1)
    results = [nudges.after_tool("bash", args, failing) for _ in range(FIX_LOOP)]
    assert all(r == failing for r in results[:-1])
    assert "failed 4 times in a row" in results[-1] and "Make IDs never reused." in results[-1]


def test_fix_loop_resets_on_success_or_another_command():
    nudges = Nudges()
    nudges.new_turn("task")
    args, failing = _bash(1)
    for _ in range(FIX_LOOP - 1):
        nudges.after_tool("bash", args, failing)
    nudges.after_tool("bash", args, "exit code 0\nok")  # passed: streak over
    assert nudges.after_tool("bash", args, failing) == failing
    for _ in range(FIX_LOOP - 2):
        nudges.after_tool("bash", args, failing)
    other = nudges.after_tool("bash", {"command": "ls"}, "exit code 2\nno such file")
    assert other == "exit code 2\nno such file"
    assert nudges.after_tool("bash", args, failing) == failing  # the streak restarted at 1
    assert nudges.after_tool("read", {"path": "x"}, "exit code 1") == "exit code 1"


def test_the_loop_adds_the_fix_loop_note_to_the_tool_result(project):
    calls = [tool_reply("bash", {"command": "exit 1"}, call_id=f"c{i}") for i in range(FIX_LOOP)]
    agent, client, _ = make(project, *calls, text_reply("Stopping."))
    list(agent.run("Fix the bug."))
    last_tool = [m for m in agent.messages if m["role"] == "tool"][-1]
    assert "failed 4 times in a row" in last_tool["content"] and "Fix the bug." in last_tool["content"]


TASK = """Add a bulk discount to the pricing module.

- Lines with at least ten units get ten percent off.
- Round the discount to cents per line.

## Tests
Keep the existing behaviour working. Add tests of your own for the new rule."""


def test_requirements_split_bullets_and_sentences_and_skip_headings():
    from tai.agent.nudges import requirements
    assert requirements(TASK) == ["Add a bulk discount to the pricing module.",
                                  "Lines with at least ten units get ten percent off.",
                                  "Round the discount to cents per line.",
                                  "Keep the existing behaviour working.",
                                  "Add tests of your own for the new rule."]
    assert requirements("Fix it.") == []


def test_recheck_once_per_turn_and_only_after_edits():
    nudges = Nudges()
    nudges.new_turn(TASK)
    assert nudges.before_done() is None  # nothing was changed: a question, not work
    nudges.after_tool("edit", {"path": "x"}, "ok")
    check = nudges.before_done()
    assert "3. Round the discount to cents per line." in check and "missing" in check
    assert nudges.before_done() is None
    nudges.new_turn("One short line.")
    nudges.after_tool("write", {"path": "x"}, "ok")
    assert nudges.before_done() is None  # fewer than two requirements: nothing to check


def test_the_loop_rechecks_before_ending_when_enabled(project):
    agent, client, _ = make(project, tool_reply("write", {"path": "b.txt", "content": "x"}),
                            text_reply("Done."), text_reply("1. ok\nAll ok."))
    events = list(agent.run(TASK))
    assert any(isinstance(e, Notice) and "requirements" in e.text for e in events)
    assert events[-1] == TurnEnd("done") and len(client.requests) == 3
    assert "Round the discount to cents per line." in client.requests[2]["messages"][-1]["content"]


def test_recheck_is_on_by_default_and_can_be_switched_off(project):
    assert make(project)[0].config.recheck
    agent, client, _ = make(project, tool_reply("write", {"path": "b.txt", "content": "x"}), text_reply("Done."))
    agent.config.recheck = False
    list(agent.run(TASK))
    assert len(client.requests) == 2
