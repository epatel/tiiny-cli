"""The benchmark harness (benchmarks/): plain and phased runs on a fake model, the sandbox, grading."""

import json
import shutil
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "benchmarks"
sys.path.insert(0, str(BENCH))

import harness  # noqa: E402
import judge  # noqa: E402
import run as bench_run  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="needs macOS sandbox-exec")

GRADE = """import sys
from pathlib import Path
from grading import Score
s = Score()
s.check((Path(sys.argv[1]) / "out.txt").read_text().strip() == "42" if (Path(sys.argv[1]) / "out.txt").exists() else False, "out.txt says 42")
s.report()
"""


def text_reply(text):
    return [{"choices": [{"delta": {"content": text}}]}, {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5}}]


def tool_reply(name, args, call_id="c1"):
    call = {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return [{"choices": [{"delta": {"tool_calls": [call]}}]}, {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}]


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat_stream(self, model, messages, **kw):
        self.requests.append([dict(m) for m in messages])
        yield from self.replies.pop(0)


@pytest.fixture
def task(tmp_path):
    folder = tmp_path / "tasks" / "answer"
    (folder / "repo").mkdir(parents=True)
    (folder / "repo" / "README.md").write_text("Write 42 to out.txt\n")
    (folder / "task.md").write_text("Write 42 to out.txt.")
    (folder / "task.json").write_text("{}")
    (folder / "grade.py").write_text(GRADE)
    return harness.Task.load(folder)


def setup(*replies, **limits):
    return harness.Setup(FakeClient(*replies), "m", "fake", 0, limits=harness.Limits(**limits), log=lambda s: None)


def test_plain_run_is_graded(task, tmp_path):
    s = setup(tool_reply("write", {"path": "out.txt", "content": "42\n"}), text_reply("Done."))
    result = harness.run_task(harness.Run(task, "plain", tmp_path / "run"), s)
    assert (result.score, result.passed, result.total) == (1.0, 1, 1)
    assert result.requests == 2 and result.prompt_tokens == 200 and result.ends == ["done"]
    assert "No user is available" in s.client.requests[0][0]["content"]
    assert "start_phases" not in json.dumps(s.client.requests)  # plain mode isn't offered phases


def test_phased_run_follows_the_plan(task, tmp_path):
    s = setup(tool_reply("phase_done", {"handoff": "read README"}),
              tool_reply("phase_done", {"handoff": "plan", "next_phases": [{"name": "build", "goal": "write it"}]}),
              tool_reply("write", {"path": "out.txt", "content": "42"}),
              tool_reply("phase_done", {"handoff": "done"}, "c2"))
    result = harness.run_task(harness.Run(task, "phased", tmp_path / "run"), s)
    assert result.phases == ["explore", "plan", "build"] and result.score == 1.0
    assert result.ends == ["phase_done"] * 3
    assert len(s.client.requests[2]) == 2  # each phase starts fresh: system + phase prompt


def test_a_phase_that_stops_early_is_nudged(task, tmp_path):
    s = setup(text_reply("Shall I go on?"), tool_reply("phase_done", {"handoff": "x"}),
              text_reply("?"), text_reply("?"), text_reply("?"), nudges=2)
    result = harness.run_task(harness.Run(task, "phased", tmp_path / "run"), s)
    assert result.phases == ["explore", "plan (done)"] and result.score == 0.0
    assert harness.NUDGE_PHASE in s.client.requests[1][-1]["content"]


def test_bash_is_sandboxed(task, tmp_path):
    outside = tmp_path / "outside.txt"
    secret = task.folder / "grade.py"
    s = setup(tool_reply("bash", {"command": f"echo x > {outside}; cat {secret}; echo 42 > out.txt"}),
              text_reply("ok"))
    result = harness.run_task(harness.Run(task, "plain", tmp_path / "run"), s)
    tool_out = s.client.requests[1][-1]["content"]
    assert not outside.exists() and "Operation not permitted" in tool_out
    assert "Score" not in tool_out  # the tasks folder (hidden tests, graders) can't be read
    assert result.score == 1.0  # writing inside the repo works


def test_file_tools_stay_inside_the_repo(task, tmp_path):
    s = setup(tool_reply("read", {"path": str(task.folder / "grade.py")}),
              tool_reply("write", {"path": str(tmp_path / "x.txt"), "content": "x"}, "c2"), text_reply("ok"))
    harness.run_task(harness.Run(task, "plain", tmp_path / "run"), s)
    assert "Outside the project" in s.client.requests[1][-1]["content"]
    assert not (tmp_path / "x.txt").exists()


def test_grader_failure_is_reported(task, tmp_path):
    (task.folder / "grade.py").write_text("raise SystemExit(3)")
    result = harness.run_task(harness.Run(task, "plain", tmp_path / "run"), setup(text_reply("ok")))
    assert result.score == 0 and result.notes[0] == "grader printed no report"


def test_schedule_alternates_the_mode_order(task):
    plan = [(m, n) for _, m, n in bench_run.schedule([task], "both", 2)]
    assert plan == [("plain", 1), ("phased", 1), ("phased", 2), ("plain", 2)]


def test_summary_table(tmp_path):
    rows = [harness.RunResult("t", "plain", 1, score=0.5, requests=4, seconds=60, prompt_tokens=1000),
            harness.RunResult("t", "phased", 1, score=1.0, requests=9, seconds=120, judge={"mean": 7.5})]
    (tmp_path / "results.jsonl").write_text("\n".join(r.to_json() for r in rows))
    assert bench_run.main(["--report", str(tmp_path)]) == 0


def test_judge_asks_the_model_with_the_rubric(task, tmp_path):
    task.judge = {"files": ["out.txt", "gone.md"], "rubric": ["plot", "prose"]}
    (tmp_path / "out.txt").write_text("forty-two")
    s = setup(text_reply('{"scores": {"1": 6, "2": 8}, "comment": "fine"}'))
    assert judge.judge(s, task, tmp_path)["mean"] == 7.0
    prompt = s.client.requests[0][0]["content"]
    assert "1. plot" in prompt and "forty-two" in prompt and "## gone.md\n\n(missing)" in prompt


def test_judge_parses_scores():
    got = judge.parse('Sure: {"scores": {"1": 7, "2": 12}, "comment": "ok"}', ["plot", "prose"])
    assert got == {"scores": {"plot": 7, "prose": 10}, "mean": 8.5, "comment": "ok"}
    assert "error" in judge.parse("no json here", ["plot"])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node for the TypeScript task")
def test_every_grader_scores_its_solution_full_and_the_start_low():
    assert bench_run.main(["--check"]) == 0
