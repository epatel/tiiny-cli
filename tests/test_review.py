"""benchmarks/review.py without Claude: pairing runs, blinding, the workspace, parsing and tallying."""

import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "benchmarks"
sys.path.insert(0, str(BENCH))

import review  # noqa: E402
from review import CRITERIA, Matchup, Pair  # noqa: E402

M = Matchup("plain", "scripted")


def _runs(tmp_path, names):
    for name in names:
        (tmp_path / "q-ts-scopes" / name / "repo").mkdir(parents=True)
    return tmp_path


def test_pairs_match_runs_by_task_and_number(tmp_path):
    run_dir = _runs(tmp_path, ["plain-1", "plain-2", "scripted-1", "scripted-3", "plain-x"])
    found = review.pairs(run_dir, M, [])
    assert [(p.task, p.number, p.a.name, p.b.name) for p in found] == [("q-ts-scopes", 1, "plain-1", "scripted-1")]
    assert review.pairs(run_dir, M, ["other"]) == []


def test_blinding_is_stable_and_unblinds_back():
    pair = Pair("t", 1, Path("a"), Path("b"))
    names = pair.shown(M)
    assert names == Pair("t", 1, Path("x"), Path("y")).shown(M)  # same task + number, same order
    assert {names["A"], names["B"]} == {"plain", "scripted"}
    verdict = {"criteria": {"task": {"winner": "A", "why": "x"}, "tests": {"winner": "tie"}}, "overall": "B"}
    out = review.unblind(verdict, names)
    assert out["criteria"]["task"]["winner"] == names["A"] and out["criteria"]["tests"]["winner"] == "tie"
    assert out["overall"] == names["B"]


def test_both_orders_occur_over_runs():
    assert {Pair("t", n, Path("a"), Path("b")).a_first for n in range(1, 11)} == {True, False}


def test_parse_verdict_finds_json_in_a_fence():
    text = 'Here it is:\n```json\n{"overall": "A", "criteria": {}}\n```'
    assert review.parse_verdict(text)["overall"] == "A"
    with pytest.raises(ValueError):
        review.parse_verdict("no verdict")


def test_prompt_names_every_criterion_and_the_test_counts():
    prompt = review.build_prompt("12/12", "8/12")
    assert all(f"- {name}:" in prompt for name in CRITERIA)
    assert "A passed 12/12, B passed 8/12" in prompt


def test_workspace_has_task_original_both_repos_and_diffs(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    (tasks / "t" / "repo").mkdir(parents=True)
    (tasks / "t" / "task.md").write_text("Do it.")
    (tasks / "t" / "repo" / "f.py").write_text("x = 1\n")
    monkeypatch.setattr(review, "TASKS", tasks)
    runs = {}
    for name, body in (("plain-1", "x = 2\n"), ("scripted-1", "x = 3\n")):
        repo = tmp_path / name / "repo"
        (repo / "node_modules").mkdir(parents=True)
        (repo / "f.py").write_text(body)
        runs[name] = repo.parent
    pair = Pair("t", 1, runs["plain-1"], runs["scripted-1"])
    ws = tmp_path / "ws"
    ws.mkdir()
    review.workspace(pair, ws)
    shown = pair.shown(M)
    expected = {"plain": "+x = 2", "scripted": "+x = 3"}
    assert expected[shown["A"]] in (ws / "A.diff").read_text()
    assert expected[shown["B"]] in (ws / "B.diff").read_text()
    assert (ws / "task.md").read_text() == "Do it." and not (ws / "A" / "node_modules").exists()


def test_tally_counts_wins_per_criterion():
    rows = [{"criteria": {"task": {"winner": "plain"}}, "overall": "plain"},
            {"criteria": {"task": {"winner": "tie"}, "focus": {"winner": "scripted"}}, "overall": "scripted"}]
    table = {name: rest for name, *rest in review.tally(rows, M)}
    assert table["task"] == [1, 0, 1] and table["focus"] == [0, 1, 1] and table["overall"] == [1, 1, 0]


def test_test_counts_read_results(tmp_path):
    (tmp_path / "results.jsonl").write_text(json.dumps({"task": "t", "mode": "plain", "run": 2, "passed": 3, "total": 4}))
    assert review.test_counts(tmp_path) == {("t", "plain", 2): "3/4"}
