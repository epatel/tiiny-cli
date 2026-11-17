"""Claude reviews benchmark runs: two variants' runs of the same task, side by side.

    .venv/bin/python benchmarks/review.py ~/.local/share/tai/bench/<run> -V plain -V scripted [-t TASK]

The hidden tests say whether a run works; they can't say whether the change is good code. Here Claude
(through the Claude Agent SDK, which uses the Claude Code login, so no API key is needed) gets the task,
the starting repo and both finished repos as ``A`` and ``B`` in random order, with read-only tools,
and says which is better on each point of ``CRITERIA``. Runs are paired by number (plain-1 with
scripted-1, ...). Choosing between two is far less noisy than a 1-10 score, and the reasons say what
to change. Verdicts go to ``review-<a>-vs-<b>.jsonl`` in the run folder; a summary is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = HERE / "tasks"
MODEL = "claude-opus-5-5"
IGNORE = shutil.ignore_patterns("node_modules", "__pycache__", ".pytest_cache", "*.pyc", ".tai")
CRITERIA = {
    "task": "Does what the task asks, precisely, including every explicit detail in it.",
    "focus": "The change is focused: no unrelated edits, nothing left behind (debug files, prints, dead code).",
    "fit": "Fits the existing code: in the right place, follows its structure, naming and style.",
    "readable": "Easy to read: clear names, small functions with one job, flat control flow, no copied logic.",
    "tests": "Tests it added or changed are meaningful and check the behaviour asked for ('tie' if neither did).",
}
PROMPT = """You review two solutions to the same programming task, as a strict senior reviewer.

The folder you are in holds:
- task.md: the task as the developer got it
- original/: the code before the change
- A/ and B/: the code after each solution; A.diff and B.diff: the changes (diff -ruN original X)

Hidden tests already measured correctness: A passed {tests_a}, B passed {tests_b}. Take that as given;
your job is the quality of the change. Read task.md, both diffs, and as much of the code as you need
(read only: don't change anything).

For each criterion, say which solution is better, or "tie" when there is no real difference. Judge each
criterion on its own evidence: a solution that is worse overall can still have the better tests or the
more focused change, and a small difference is a tie.
{criteria}

Answer with only a JSON object, no other text:
{{"criteria": {{"<criterion>": {{"winner": "A" | "B" | "tie", "why": "<one sentence>"}}, ...}},
  "overall": "A" | "B" | "tie",
  "summary": "<two sentences: the most important difference>"}}
"""


@dataclass
class Matchup:
    """The two variants compared, and the reviewing model."""
    a: str
    b: str
    model: str = MODEL


@dataclass
class Pair:
    task: str
    number: int
    a: Path  # run folder of variant a
    b: Path

    @property
    def a_first(self) -> bool:
        """Whether variant a is shown as "A" (random, but the same on every review of this pair)."""
        return random.Random(f"{self.task}-{self.number}").random() < 0.5

    def shown(self, matchup: Matchup) -> dict[str, str]:
        """Label -> variant name."""
        first, second = (matchup.a, matchup.b) if self.a_first else (matchup.b, matchup.a)
        return {"A": first, "B": second, "tie": "tie"}


def pairs(run_dir: Path, matchup: Matchup, tasks: list[str]) -> list[Pair]:
    """Runs of both variants with the same task and number."""
    a, b = matchup.a, matchup.b
    found = []
    for task_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        if tasks and task_dir.name not in tasks:
            continue
        for run_a in sorted(task_dir.glob(f"{a}-*")):
            number = run_a.name.rsplit("-", 1)[1]
            run_b = task_dir / f"{b}-{number}"
            if number.isdigit() and run_b.is_dir():
                found.append(Pair(task_dir.name, int(number), run_a, run_b))
    return found


def test_counts(run_dir: Path) -> dict[tuple[str, str, int], str]:
    """(task, variant, run) -> "passed/total" from results.jsonl."""
    counts = {}
    for line in (run_dir / "results.jsonl").read_text().splitlines():
        r = json.loads(line)
        counts[(r["task"], r["mode"], r["run"])] = f"{r.get('passed', '?')}/{r.get('total', '?')}"
    return counts


def build_prompt(tests_a: str, tests_b: str) -> str:
    criteria = "\n".join(f"- {name}: {text}" for name, text in CRITERIA.items())
    return PROMPT.format(tests_a=tests_a, tests_b=tests_b, criteria=criteria)


def workspace(pair: Pair, into: Path) -> None:
    """task.md, original/, A/, B/ and the two diffs; which variant is A depends on ``pair.a_first``."""
    task = TASKS / pair.task
    shutil.copy(task / "task.md", into / "task.md")
    shutil.copytree(task / "repo", into / "original", ignore=IGNORE)
    for label, run in zip("AB", (pair.a, pair.b) if pair.a_first else (pair.b, pair.a)):
        shutil.copytree(run / "repo", into / label, ignore=IGNORE)
        diff = subprocess.run(["diff", "-ruN", "original", label], cwd=into, capture_output=True, text=True)
        (into / f"{label}.diff").write_text(diff.stdout)


def parse_verdict(text: str) -> dict:
    """The JSON object in Claude's answer (it may be wrapped in a code fence)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON in the review: {text[:200]!r}")
    return json.loads(m.group(0))


def unblind(verdict: dict, names: dict[str, str]) -> dict:
    """Replace "A"/"B" by variant names (``Pair.shown``)."""
    criteria = {k: {**v, "winner": names.get(v.get("winner"), "tie")} for k, v in verdict.get("criteria", {}).items()}
    return {**verdict, "criteria": criteria, "overall": names.get(verdict.get("overall"), "tie")}


async def ask_claude(prompt: str, cwd: Path, model: str = MODEL) -> tuple[str, float]:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(cwd=str(cwd), model=model, allowed_tools=["Read", "Grep", "Glob"],
                                 disallowed_tools=["Write", "Edit", "Bash", "WebFetch", "WebSearch", "Task"],
                                 max_turns=40)
    result = None
    async for message in query(prompt=prompt, options=options):  # read to the end: leaving early breaks the SDK
        if isinstance(message, ResultMessage):
            result = message
    if result is None or result.is_error:
        raise RuntimeError(f"review failed: {result.result if result else 'no result'}")
    return result.result or "", result.total_cost_usd or 0.0


def review(pair: Pair, tests: dict, matchup: Matchup) -> dict:
    names = pair.shown(matchup)
    tests_a, tests_b = (tests.get((pair.task, names[label], pair.number), "?") for label in "AB")
    with tempfile.TemporaryDirectory(prefix="tai-review-") as tmp:
        workspace(pair, Path(tmp))
        text, cost = asyncio.run(ask_claude(build_prompt(tests_a, tests_b), Path(tmp), matchup.model))
    verdict = unblind(parse_verdict(text), names)
    return {"task": pair.task, "run": pair.number, "a": matchup.a, "b": matchup.b,
            "a_was": "A" if pair.a_first else "B", "cost_usd": round(cost, 3), **verdict}


def tally(rows: list[dict], matchup: Matchup) -> list[tuple[str, int, int, int]]:
    """Per criterion and overall: (name, wins a, wins b, ties)."""
    lines = []
    for name in [*CRITERIA, "overall"]:
        won = Counter(r["overall"] if name == "overall" else r["criteria"].get(name, {}).get("winner", "tie")
                      for r in rows)
        lines.append((name, won[matchup.a], won[matchup.b], won["tie"]))
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("run_dir", type=Path, help="a benchmark output folder")
    p.add_argument("-V", "--variant", action="append", required=True, help="the two variants to compare")
    p.add_argument("-t", "--task", action="append", default=[], help="only these tasks")
    p.add_argument("--model", default=MODEL, help=f"the reviewing model (default {MODEL})")
    args = p.parse_args(argv)
    if len(args.variant) != 2:
        p.error("give exactly two -V")
    matchup = Matchup(*args.variant, model=args.model)
    a, b = matchup.a, matchup.b
    run_dir = args.run_dir.expanduser()
    todo = pairs(run_dir, matchup, args.task)
    if not todo:
        p.error(f"no runs of both {a} and {b} with the same number in {run_dir}")
    tests, rows = test_counts(run_dir), []
    out = run_dir / f"review-{a}-vs-{b}.jsonl"
    for pair in todo:
        print(f"{pair.task} run {pair.number} …", file=sys.stderr, flush=True)
        row = review(pair, tests, matchup)
        rows.append(row)
        with out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"  → {row['overall']}: {row.get('summary', '')}", file=sys.stderr, flush=True)
    print(f"\n{'criterion':<10} {a:>10} {b:>10} {'tie':>5}")
    for name, wins_a, wins_b, ties in tally(rows, matchup):
        print(f"{name:<10} {wins_a:>10} {wins_b:>10} {ties:>5}")
    print(f"\n{len(rows)} pairs, verdicts in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
