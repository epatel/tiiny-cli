#!/usr/bin/env python3
"""Benchmark `tai agent` with and without phased work on the device.

    .venv/bin/python benchmarks/run.py                       # every task, plain + phased, 1 run each
    .venv/bin/python benchmarks/run.py -t ts-templates -n 3  # one task, 3 runs per mode
    .venv/bin/python benchmarks/run.py --judge               # also let the model score writing/research
    .venv/bin/python benchmarks/run.py --check               # check the graders (no device)
    .venv/bin/python benchmarks/run.py --report DIR          # summarise an earlier run

See benchmarks/README.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from judge import judge  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

out = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark tai agent: plain vs phased work.")
    p.add_argument("-t", "--task", action="append", help="task name (repeatable; default: all)")
    p.add_argument("--mode", choices=("plain", "phased", "both"), default="both")
    p.add_argument("-n", "--runs", type=int, default=1, help="runs per task and mode (default 1)")
    p.add_argument("-m", "--model", help="agent model (default: tai's default agent model)")
    p.add_argument("--judge", action="store_true", help="score tasks with a rubric using the model as judge")
    p.add_argument("--timeout", type=float, default=60, help="minutes per run (default 60)")
    p.add_argument("--out", type=Path, help="results folder (default: ~/.local/share/tai/bench/<time>)")
    p.add_argument("-y", "--yes", action="store_true", help="unload other models without asking")
    p.add_argument("--check", action="store_true", help="check every grader: baseline low, solution 100%%")
    p.add_argument("--report", type=Path, metavar="DIR", help="summarise the results in DIR and exit")
    return p.parse_args(argv)


def pick_tasks(names: list[str] | None) -> list[harness.Task]:
    tasks = harness.all_tasks()
    if not names:
        return tasks
    unknown = set(names) - {t.name for t in tasks}
    if unknown:
        raise SystemExit(f"unknown task(s): {', '.join(sorted(unknown))}; have: {', '.join(t.name for t in tasks)}")
    return [t for t in tasks if t.name in names]


# -- grader check ---------------------------------------------------------------------
def check(tasks: list[harness.Task]) -> int:
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for task in tasks:
            res = harness.check_grader(task, Path(tmp) / task.name)
            base, sol = res["baseline"], res["solution"]
            ok = sol.score == 1.0 and base.score < 0.5 and base.total == sol.total
            bad += not ok
            mark = "[green]ok[/]" if ok else "[red]FAIL[/]"
            out.print(f"{mark}  {task.name}: baseline {base.passed}/{base.total}, solution {sol.passed}/{sol.total}")
            if not ok:
                out.print("    " + "\n    ".join(n[:200] for n in sol.notes[:8]), highlight=False)
    return 1 if bad else 0


# -- device runs ----------------------------------------------------------------------
def device_setup(args: argparse.Namespace) -> harness.Setup:
    from tai.agent.command import AGENT_THINK_SETTINGS
    from tai.cli import App, _think_extra

    app = App(device=None, yes=args.yes)
    model = app.agent_model(args.model)
    app.ensure_loaded(model)
    extra = _think_extra(app, model, None, settings=AGENT_THINK_SETTINGS)
    limits = harness.Limits(timeout=args.timeout * 60)
    return harness.Setup(app.client, model.id, model.name, model.context, extra, limits, log=log)


def log(text: str) -> None:
    out.print(f"[dim]{time.strftime('%H:%M:%S')}[/] {text}", highlight=False)


def schedule(tasks: list[harness.Task], mode: str, runs: int) -> list[tuple[harness.Task, str, int]]:
    """Every (task, mode, run); the mode order alternates between runs so neither mode always goes first."""
    modes = list(harness.MODES) if mode == "both" else [mode]
    plan = []
    for n in range(1, runs + 1):
        order = modes if n % 2 else modes[::-1]
        plan += [(task, m, n) for task in tasks for m in order]
    return plan


def run_all(args: argparse.Namespace, tasks: list[harness.Task]) -> Path:
    folder = args.out or harness_home() / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    setup = device_setup(args)
    (folder / "meta.json").write_text(json.dumps({"model": setup.model_id, "context": setup.context_window,
                                                  "extra": setup.extra, "started": time.time()}))
    log(f"model {setup.model_id}; results in {folder}")
    for task, mode, n in schedule(tasks, args.mode, args.runs):
        log(f"[bold]{task.name}[/] · {mode} · run {n}")
        run = harness.Run(task, mode, folder / task.name / f"{mode}-{n}", n)
        result = harness.run_task(run, setup)
        if args.judge and task.judge:
            result.judge = judge(setup, task, run.folder / "repo")
        with open(folder / "results.jsonl", "a") as f:
            f.write(result.to_json() + "\n")
        log(f"  → {result.passed}/{result.total} ({result.score:.0%}), {result.requests} requests, "
            f"{result.seconds / 60:.1f} min" + (f", judge {result.judge.get('mean')}" if result.judge else ""))
    return folder


def harness_home() -> Path:
    from tai.config import data_dir
    return data_dir() / "bench"


# -- report ---------------------------------------------------------------------------
def load_results(folder: Path) -> list[dict]:
    path = folder / "results.jsonl"
    if not path.is_file():
        raise SystemExit(f"no results.jsonl in {folder}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def summary(results: list[dict]) -> Table:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        groups[(r["task"], r["mode"])].append(r)
    table = Table(title="tai agent: plain vs phased")
    for col in ("task", "mode", "runs", "score", "range", "judge", "tokens", "requests", "minutes"):
        table.add_column(col, justify="left" if col in ("task", "mode") else "right")
    for (task, mode), rows in sorted(groups.items()):
        table.add_row(task, mode, *row_stats(rows))
    return table


def row_stats(rows: list[dict]) -> list[str]:
    scores = [r["score"] for r in rows]
    judged = [r["judge"]["mean"] for r in rows if (r.get("judge") or {}).get("mean") is not None]
    tokens = [r["prompt_tokens"] + r["completion_tokens"] for r in rows]
    return [str(len(rows)), f"{statistics.mean(scores):.0%}", f"{min(scores):.0%}–{max(scores):.0%}",
            f"{statistics.mean(judged):.1f}" if judged else "–", f"{statistics.mean(tokens) / 1000:.0f}k",
            f"{statistics.mean(r['requests'] for r in rows):.0f}",
            f"{statistics.mean(r['seconds'] for r in rows) / 60:.1f}"]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.report:
        out.print(summary(load_results(args.report)))
        return 0
    tasks = pick_tasks(args.task)
    if args.check:
        return check(tasks)
    folder = run_all(args, tasks)
    out.print(summary(load_results(folder)))
    out.print(f"Runs are kept in {folder} (each run's repo, work folder and results.jsonl).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
