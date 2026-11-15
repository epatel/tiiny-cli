"""The benchmark harness (benchmarks/): plain and phased runs on a fake model, the sandbox, grading."""

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "benchmarks"
sys.path.insert(0, str(BENCH))

import harness  # noqa: E402
import judge  # noqa: E402
import run as bench_run  # noqa: E402
import process  # noqa: E402
from variants import Variant  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="needs macOS sandbox-exec")

GRADE = """import sys
import time
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


def test_every_request_is_logged_with_what_it_added_and_what_came_back(task, tmp_path):
    done = text_reply("Done.")
    done[-1]["timings"] = {"predicted_n": 30, "predicted_ms": 2000.0}
    s = setup(tool_reply("write", {"path": "out.txt", "content": "42\n"}), done)
    result = harness.run_task(harness.Run(task, "plain", tmp_path / "run"), s)
    lines = [json.loads(x) for x in (tmp_path / "run" / "requests.jsonl").read_text().splitlines()]
    assert [x["kind"] for x in lines] == ["write", "text"] and [x["fresh"] for x in lines] == [True, False]
    assert lines[0]["messages"][0]["role"] == "system" and lines[0]["reply"]["tool_calls"][0]["name"] == "write"
    assert [m["role"] for m in lines[1]["messages"]] == ["assistant", "tool"]  # only what's new
    assert lines[1]["reply"]["text"] == "Done." and lines[1]["timings"]["predicted_n"] == 30
    assert result.output_by == {"write": 20, "text": 5} and result.write_speed == 15.0
    assert result.model == "fake"


def test_phased_run_follows_the_plan(task, tmp_path):
    s = setup(tool_reply("phase_done", {"handoff": "read README"}),
              tool_reply("phase_done", {"handoff": "plan", "next_phases": [{"name": "build", "goal": "write it"}]}),
              tool_reply("write", {"path": "out.txt", "content": "42"}),
              tool_reply("phase_done", {"handoff": "done"}, "c2"))
    result = harness.run_task(harness.Run(task, "phased", tmp_path / "run"), s)
    assert result.phases == ["explore", "plan", "build"] and result.score == 1.0
    assert result.ends == ["phase_done"] * 3
    assert len(s.client.requests[2]) == 2  # each phase starts fresh: system + phase prompt
    lines = (tmp_path / "run" / "requests.jsonl").read_text().splitlines()
    assert [json.loads(x)["fresh"] for x in lines] == [True, True, True, False]


def test_a_phase_that_stops_early_is_nudged(task, tmp_path):
    s = setup(text_reply("Shall I go on?"), tool_reply("phase_done", {"handoff": "x"}),
              text_reply("?"), text_reply("?"), text_reply("?"), nudges=2)
    result = harness.run_task(harness.Run(task, "phased", tmp_path / "run"), s)
    assert result.phases == ["explore", "plan (done)"] and result.score == 0.0
    assert harness.NUDGE_PHASE in s.client.requests[1][-1]["content"]


class SlowClient(FakeClient):
    def chat_stream(self, model, messages, **kw):
        time.sleep(0.3)
        yield from super().chat_stream(model, messages, **kw)


def test_a_run_past_the_time_limit_is_cancelled_and_marked(task, tmp_path):
    s = setup(text_reply("x"), timeout=0.05)
    s.client = SlowClient(tool_reply("write", {"path": "out.txt", "content": "42"}), text_reply("Done."))
    result = harness.run_task(harness.Run(task, "plain", tmp_path / "run"), s)
    assert result.timed_out and result.ends == ["interrupted"]
    assert harness.RunResult.from_json(result.to_json()).timed_out


def test_timeout_zero_means_no_limit(task, tmp_path):
    s = setup(tool_reply("write", {"path": "out.txt", "content": "42"}), text_reply("Done."), timeout=0)
    result = harness.run_task(harness.Run(task, "plain", tmp_path / "run"), s)
    assert not result.timed_out and result.score == 1.0


def test_a_reply_without_usage_is_a_device_error(task, tmp_path):
    """Seen on the device: a reply cut off mid tool call, then empty replies. Not the agent's fault."""
    s = setup([{"choices": [{"delta": {"content": ""}}]}])
    result = harness.run_task(harness.Run(task, "plain", tmp_path / "run"), s)
    assert result.device_errors == 1 and result.errored
    assert not harness.RunResult("t", "plain", 1, device_errors=1, timed_out=True).errored  # cancelled by the limit


def test_a_run_the_device_broke_is_kept_and_retried(task, tmp_path, monkeypatch):
    monkeypatch.setattr(bench_run, "RETRY_WAIT", 0)
    s = setup([{"choices": []}], tool_reply("write", {"path": "out.txt", "content": "42"}), text_reply("Done."))
    args = bench_run.parse_args([])
    bench_run.run_one(args, s, tmp_path, harness.Run(task, "plain", tmp_path / "plain-1"))
    rows = bench_run.load_results(tmp_path)
    assert [r.errored for r in rows] == [True, False] and rows[1].score == 1.0
    assert (tmp_path / "plain-1-error" / "requests.jsonl").is_file()


def test_a_broken_run_never_overwrites_an_earlier_one(tmp_path):
    (tmp_path / "run-1").mkdir()
    (tmp_path / "run-1-error").mkdir()
    (tmp_path / "run-1-error" / "x").write_text("x")
    assert bench_run.keep_broken(tmp_path / "run-1").name == "run-1-error-2"


def test_resume_skips_finished_runs_but_not_broken_ones(task, tmp_path, monkeypatch):
    rows = [harness.RunResult(task.name, "plain", 1, model="fake", score=1.0),
            harness.RunResult(task.name, "plain", 2, model="fake", ends=["error"])]
    (tmp_path / "results.jsonl").write_text("\n".join(r.to_json() for r in rows))
    monkeypatch.setattr(bench_run, "device_setup", lambda args, query: setup())
    ran = []
    monkeypatch.setattr(bench_run, "run_one", lambda args, s, folder, run: ran.append(run.number))
    args = bench_run.parse_args(["--out", str(tmp_path), "--resume", "-n", "3", "-V", "plain"])
    bench_run.run_all(args, [task])
    assert ran == [2, 3]


def test_the_report_leaves_out_runs_the_device_broke(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(bench_run.out, "width", 200)
    rows = [harness.RunResult("t", "plain", 1, score=1.0), harness.RunResult("t", "plain", 2, score=0.0, ends=["error"])]
    (tmp_path / "results.jsonl").write_text("\n".join(r.to_json() for r in rows))
    assert bench_run.main(["--report", str(tmp_path)]) == 0
    shown = capsys.readouterr().out
    assert "100%–100%" in shown and "Left out" in shown and "t · plain · run 2" in shown


def test_older_results_count_a_cancelled_last_turn_as_timed_out():
    def old(ends):
        return harness.RunResult.from_json(json.dumps({"task": "t", "mode": "plain", "run": 1, "ends": ends}))
    assert old(["phase_done", "interrupted"]).timed_out
    assert not old(["done"]).timed_out and not old([]).timed_out


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


def test_schedule_alternates_the_variant_order(task):
    variants = bench_run.pick_variants(bench_run.parse_args([]))
    plan = [(v.name, n) for _, v, n in bench_run.schedule([task], variants, 2)]
    assert plan == [("plain", 1), ("phased", 1), ("phased", 2), ("plain", 2)]
    assert [v.name for v in bench_run.pick_variants(bench_run.parse_args(["-V", "lean", "-V", "checklist"]))] == \
        ["lean", "checklist"]


def test_checklist_mode_reviews_after_the_work(task, tmp_path):
    s = setup(tool_reply("write", {"path": "out.txt", "content": "42"}), text_reply("- [ ] 42 in out.txt"),
              text_reply("- [x] 42 in out.txt"))
    result = harness.run_task(harness.Run(task, "checklist", tmp_path / "run"), s)
    assert harness.CHECKLIST.strip() in s.client.requests[0][-1]["content"]
    assert s.client.requests[2][-1]["content"] == harness.REVIEW
    assert result.phases == ["work", "review"] and result.score == 1.0


def test_a_variant_adds_its_prompt_and_switches_thinking_per_stage(task, tmp_path):
    variant = Variant("v", "phased", prompt="\nBE BRIEF", think={"plan": True}, stop_after="plan")
    s = setup(tool_reply("phase_done", {"handoff": "read"}),
              tool_reply("phase_done", {"handoff": "plan", "next_phases": [{"name": "build", "goal": "x"}]}))
    s.extra = {"chat_template_kwargs": {"enable_thinking": False}}
    seen = []
    chat = s.client.chat_stream
    s.client.chat_stream = lambda model, messages, **kw: (seen.append(kw), (yield from chat(model, messages, **kw)))[1]
    result = harness.run_task(harness.Run(task, "v", tmp_path / "run", variant=variant), s)
    assert result.phases == ["explore", "plan"] and result.mode == "v"  # stopped after the plan
    assert [k["chat_template_kwargs"]["enable_thinking"] for k in seen] == [False, True]
    assert "BE BRIEF" in s.client.requests[0][0]["content"]


def test_a_variant_extends_another_and_adds_a_prompt_per_domain(tmp_path):
    (tmp_path / "base.json").write_text('{"mode": "checklist", "prompt": "A", "think": {"work": true}}')
    (tmp_path / "kid.json").write_text(json.dumps({"extends": str(tmp_path / "base.json"), "prompt": "B",
                                                   "prompt_by_domain": {"python": "P"}}))
    v = Variant.load(str(tmp_path / "kid.json"))
    assert (v.name, v.mode, v.think) == ("kid", "checklist", {"work": True})
    assert v.prompt_for("python") == "ABP" and v.prompt_for("writing") == "AB"
    idioms = Variant.load("lean-idioms")
    assert Variant.load("lean").prompt in idioms.prompt and "Code shape" in idioms.prompt
    assert "TypeScript idioms" in idioms.prompt_for("typescript") and "Python" not in idioms.prompt_for("typescript")


def test_style_is_in_the_agent_prompt_unless_a_variant_turns_it_off(task, tmp_path):
    from tai.agent.prompt import STYLE
    assert not Variant.load("lean-shape").style and Variant.load("plain").style  # inherited from lean
    for name, expected in (("plain", True), ("bare", False)):
        s = setup(text_reply("done"))
        harness.run_task(harness.Run(task, name, tmp_path / name, variant=Variant.load(name)), s)
        assert (STYLE in s.client.requests[0][0]["content"]) is expected


def test_process_metrics(task, tmp_path):
    task.deliverables = ["out.txt"]
    s = setup(tool_reply("write", {"path": "scratch.py", "content": "print(1)"}),
              tool_reply("write", {"path": "out.txt", "content": "4"}, "c2"),
              tool_reply("write", {"path": "out.txt", "content": "42"}, "c3"), text_reply("Done."))
    p = harness.run_task(harness.Run(task, "plain", tmp_path / "run"), s).process
    assert p["strays"] == ["scratch.py"] and p["rewrites"] == 1
    assert p["overhead"] == round(1 - 40 / 65, 3)  # 3 writes of 20 tokens + 5 for the answer; 2 were deliverables
    assert p["first_deliverable"] is not None and p["health"] is None  # not a code task


def test_code_health_scores_only_changed_source_files(tmp_path):
    start, repo = tmp_path / "start", tmp_path / "repo"
    for root in (start, repo):
        (root / "src").mkdir(parents=True)
        (root / "src" / "same.py").write_text("x = 1\n")
    (repo / "src" / "new.py").write_text("y = 2\n")
    report = {"files": [{"path": "src/same.py", "score": 4.0, "loc": 100},  # unchanged: not the run's work
                        {"path": "src/new.py", "score": 8.0, "loc": 30},
                        {"path": "src/old.py", "score": 6.0, "loc": 10},  # changed (start lacks it)
                        {"path": "tests/test_new.py", "score": 10.0, "loc": 50}]}
    for name in ("old.py",):
        (repo / "src" / name).write_text("z = 3\n")
    assert process.code_health(report, repo, start) == round((8 * 30 + 6 * 10) / 40, 2)
    assert process.code_health({"files": report["files"][:1]}, repo, start) is None
    assert all(process.TEST_FILE.search(p) for p in ("tests/a.py", "test_a.py", "src/a.test.ts", "conftest.py"))
    assert not process.TEST_FILE.search("src/contest.py")


def test_code_columns_count_passing_runs_only(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(bench_run.out, "width", 250)
    rows = [harness.RunResult("t", "plain", 1, score=1.0, process={"code": 7.5, "code_ref": 8.25}),
            harness.RunResult("t", "plain", 2, score=0.2, process={"code": 2.0, "code_ref": 8.25})]
    (tmp_path / "results.jsonl").write_text("\n".join(r.to_json() for r in rows))
    assert bench_run.main(["--report", str(tmp_path)]) == 0
    shown = capsys.readouterr().out
    assert "7.50" in shown and "8.25" in shown and "4.75" not in shown


def test_summary_table(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(bench_run.out, "width", 200)
    rows = [harness.RunResult("t", "plain", 1, score=0.5, requests=4, seconds=60, prompt_tokens=1000,
                              model="A", output_by={"write": 300, "text": 100}, write_speed=20.0),
            harness.RunResult("t", "phased", 1, score=1.0, requests=9, seconds=120, judge={"mean": 7.5}, model="B")]
    (tmp_path / "results.jsonl").write_text("\n".join(r.to_json() for r in rows))
    assert bench_run.main(["--report", str(tmp_path)]) == 0
    shown = capsys.readouterr().out
    assert "written tokens by reply kind" in shown and "75%" in shown and "20.0" in shown


def test_older_results_take_the_model_from_meta(tmp_path):
    (tmp_path / "results.jsonl").write_text(harness.RunResult("t", "plain", 1).to_json())
    (tmp_path / "meta.json").write_text('{"model": "Qwen/Qwen3.8-27B"}')
    assert bench_run.load_results(tmp_path)[0].model == "Qwen3.8-27B"


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
