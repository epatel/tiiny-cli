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
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from judge import judge  # noqa: E402
from variants import Variant  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

out = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark tai agent: plain vs phased work.")
    p.add_argument("-t", "--task", action="append", help="task name (repeatable; default: all of the suite)")
    p.add_argument("--quick", action="store_true", help="the quick suite: small tasks, minutes each, for iterating")
    p.add_argument("--mode", choices=("plain", "phased", "both"), default="both", help="the default variants")
    p.add_argument("-V", "--variant", action="append",
                   help="a variant from benchmarks/variants/ or a .json path (repeatable; replaces --mode)")
    p.add_argument("-n", "--runs", type=int, default=1, help="runs per task and mode (default 1)")
    p.add_argument("-m", "--model", action="append",
                   help="agent model (repeatable, to compare models; default: tai's default agent model)")
    p.add_argument("--judge", action="store_true", help="score tasks with a rubric using the model as judge")
    p.add_argument("--timeout", type=float, default=60, help="minutes per run (default 60; 0 = no limit)")
    p.add_argument("--out", type=Path, help="results folder (default: ~/.local/share/tai/bench/<time>)")
    p.add_argument("--resume", action="store_true",
                   help="with --out: skip the runs that already have a result there (continue a stopped benchmark)")
    p.add_argument("-y", "--yes", action="store_true", help="unload other models without asking")
    p.add_argument("--check", action="store_true", help="check every grader: baseline low, solution 100%%")
    p.add_argument("--report", type=Path, metavar="DIR", help="summarise the results in DIR and exit")
    return p.parse_args(argv)


def pick_tasks(names: list[str] | None, quick: bool = False) -> list[harness.Task]:
    if not names:
        return harness.all_tasks("quick" if quick else "full")
    tasks = harness.all_tasks()
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
def device_setup(args: argparse.Namespace, query: str | None) -> harness.Setup:
    from tai.agent.command import AGENT_THINK_SETTINGS
    from tai.cli import App, _think_extra

    app = App(device=None, yes=args.yes)
    model = app.agent_model(query)
    app.ensure_loaded(model)
    extra = _think_extra(app, model, None, settings=AGENT_THINK_SETTINGS)
    limits = harness.Limits(timeout=args.timeout * 60)
    return harness.Setup(app.client, model.id, model.name, model.context, extra, limits, log=log)


def log(text: str) -> None:
    out.print(f"[dim]{time.strftime('%H:%M:%S')}[/] {text}", highlight=False)


def schedule(tasks: list[harness.Task], variants: list[Variant], runs: int) -> list[tuple[harness.Task, Variant, int]]:
    """Every (task, variant, run); the order alternates between runs so no variant always goes first."""
    plan = []
    for n in range(1, runs + 1):
        order = variants if n % 2 else variants[::-1]
        plan += [(task, v, n) for task in tasks for v in order]
    return plan


def pick_variants(args: argparse.Namespace) -> list[Variant]:
    names = args.variant or (list(harness.MODES) if args.mode == "both" else [args.mode])
    return [Variant.load(n) for n in names]


def run_all(args: argparse.Namespace, tasks: list[harness.Task]) -> Path:
    folder = args.out or harness_home() / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    done = finished(folder) if args.resume else set()
    models = []
    for query in args.model or [None]:  # one model at a time: each is loaded once, for all its runs
        setup = device_setup(args, query)
        models.append({"model": setup.model_id, "context": setup.context_window, "extra": setup.extra})
        (folder / "meta.json").write_text(json.dumps({"models": models, "started": time.time()}))
        log(f"model {setup.model_id}; results in {folder}")
        base = folder / setup.model_name if len(args.model or []) > 1 else folder  # a folder per compared model
        for task, v, n in schedule(tasks, pick_variants(args), args.runs):
            if (setup.model_name, task.name, v.name, n) in done:
                continue
            run_one(args, setup, folder, harness.Run(task, v.name, base / task.name / f"{v.name}-{n}", n, v))
    return folder


def finished(folder: Path) -> set[tuple[str, str, str, int]]:
    """(model, task, variant, run) of every result in ``folder`` the device didn't break."""
    if not (folder / "results.jsonl").is_file():
        return set()
    return {(r.model, r.task, r.mode, r.run) for r in load_results(folder) if not r.errored}


RETRY_WAIT = 60  # seconds: the device has recovered from a broken reply within ~10 s


def run_one(args: argparse.Namespace, setup: harness.Setup, folder: Path, run: harness.Run) -> None:
    """Run and record; a run the device broke is kept as <folder>-error and run once more."""
    log(f"[bold]{run.task.name}[/] · {run.mode} · run {run.number}")
    result = attempt(args, setup, folder, run)
    if result.errored:
        kept = keep_broken(run.folder)
        log(f"  [yellow]device error: kept as {kept.name}, retrying in {RETRY_WAIT} s[/]")
        time.sleep(RETRY_WAIT)
        attempt(args, setup, folder, run)


def keep_broken(folder: Path) -> Path:
    """Move a broken run aside to the first free <folder>-error[-n]."""
    kept, n = folder.with_name(folder.name + "-error"), 1
    while kept.exists():
        n += 1
        kept = folder.with_name(f"{folder.name}-error-{n}")
    return folder.rename(kept)


def attempt(args: argparse.Namespace, setup: harness.Setup, folder: Path, run: harness.Run) -> harness.RunResult:
    result = harness.run_task(run, setup)
    if args.judge and run.task.judge:
        result.judge = judge(setup, run.task, run.folder / "repo")
    with open(folder / "results.jsonl", "a") as f:
        f.write(result.to_json() + "\n")
    log(f"  → {result.passed}/{result.total} ({result.score:.0%}), {result.requests} requests, "
        f"{result.seconds / 60:.1f} min, {result.write_speed} tok/s"
        + (f", judge {result.judge.get('mean')}" if result.judge else "")
        + (" [yellow]· timed out[/]" if result.timed_out else "")
        + (f" [yellow]· {result.device_errors} device errors[/]" if result.errored else ""))
    return result


def harness_home() -> Path:
    from tai.config import data_dir
    return data_dir() / "bench"


# -- report ---------------------------------------------------------------------------
def load_results(folder: Path) -> list[harness.RunResult]:
    path = folder / "results.jsonl"
    if not path.is_file():
        raise SystemExit(f"no results.jsonl in {folder}")
    results = [harness.RunResult.from_json(line) for line in path.read_text().splitlines() if line.strip()]
    meta = folder / "meta.json"
    older = json.loads(meta.read_text()).get("model", "") if meta.is_file() else ""  # before results had a model
    for r in results:
        r.model = r.model or older.split("/")[-1]
    return results


def summary(results: list[harness.RunResult]) -> Table:
    table = Table(title="tai agent: plain vs phased")
    cols = ("task", "model", "mode", "runs", "timed out", "score", "range", "judge", "tokens", "requests",
            "minutes", "tok/s")
    for col in cols:
        table.add_column(col, justify="left" if col in ("task", "model", "mode") else "right")
    for (task, model, mode), rows in grouped(results):
        table.add_row(task, model or "?", mode, *row_stats(rows))
    return table


def grouped(results: list[harness.RunResult]) -> list[tuple[tuple[str, str, str], list[harness.RunResult]]]:
    groups: dict[tuple[str, str, str], list[harness.RunResult]] = defaultdict(list)
    for r in results:
        groups[(r.task, r.model, r.mode)].append(r)
    return sorted(groups.items())


def output_table(results: list[harness.RunResult]) -> Table:
    """Where the written tokens went: share per reply kind (a tool, or plain text)."""
    kinds = Counter()
    for r in results:
        kinds.update(r.output_by)
    top = [k for k, _ in kinds.most_common(7)]
    table = Table(title="written tokens by reply kind")
    for col in ("task", "model", "mode", "written", *top, "other"):
        table.add_column(col, justify="left" if col in ("task", "model", "mode") else "right")
    for (task, model, mode), rows in grouped(results):
        table.add_row(task, model or "?", mode, *output_cells(rows, top))
    return table


def output_cells(rows: list[harness.RunResult], top: list[str]) -> list[str]:
    """Written tokens per run, then each kind's share of them (``top`` first, the rest as other)."""
    by = Counter()
    for r in rows:
        by.update(r.output_by)
    total = sum(by.values())
    by["other"] = total - sum(by[k] for k in top)
    return [f"{total / len(rows) / 1000:.1f}k", *(f"{by[k] / total:.0%}" if by[k] else "–" for k in (*top, "other"))]


def row_stats(rows: list[harness.RunResult]) -> list[str]:
    scores = [r.score for r in rows]
    return [str(len(rows)), timed_out_cell(rows), f"{statistics.mean(scores):.0%}",
            f"{min(scores):.0%}–{max(scores):.0%}", judge_cell(rows),
            f"{statistics.mean(r.prompt_tokens + r.completion_tokens for r in rows) / 1000:.0f}k",
            f"{statistics.mean(r.requests for r in rows):.0f}", f"{statistics.mean(r.seconds for r in rows) / 60:.1f}",
            speed_cell(rows)]


def speed_cell(rows: list[harness.RunResult]) -> str:
    speeds = [r.write_speed for r in rows if r.write_speed]
    return f"{statistics.mean(speeds):.1f}" if speeds else "–"


def judge_cell(rows: list[harness.RunResult]) -> str:
    judged = [r.judge["mean"] for r in rows if (r.judge or {}).get("mean") is not None]
    return f"{statistics.mean(judged):.1f}" if judged else "–"


def timed_out_cell(rows: list[harness.RunResult]) -> str:
    cut = sum(r.timed_out for r in rows)
    return f"[yellow]{cut}[/]" if cut else "–"


def report(results: list[harness.RunResult]) -> None:
    kept = [r for r in results if not r.errored]
    out.print(summary(kept))
    if any(r.output_by for r in kept):
        out.print(output_table(kept))
    if any(r.process for r in kept):
        out.print(process_table(kept))
    left_out([r for r in results if r.errored])


def left_out(broken: list[harness.RunResult]) -> None:
    """Runs the device broke: their scores say nothing about the agent."""
    if broken:
        names = ", ".join(f"{r.task} · {r.mode} · run {r.run}" for r in broken)
        out.print(f"Left out, the device failed during the run: {names}")


def process_table(results: list[harness.RunResult]) -> Table:
    """How the runs worked (means per group; see benchmarks/process.py)."""
    table = Table(title="process (lower overhead, rewrites and strays are better)")
    for col in ("task", "model", "mode", "health", "code", "ref", "smells +/-", "overhead", "rewrites", "strays",
                "1st deliverable"):
        table.add_column(col, justify="left" if col in ("task", "model", "mode") else "right")
    for (task, model, mode), rows in grouped(results):
        cells = process_cells([r.process for r in rows if r.process])
        table.add_row(task, model or "?", mode, cells[0], *code_cells(rows), *cells[1:])
    return table


PASSING = 0.8  # code health counts only for runs that pass this share of the tests


def code_cells(rows: list[harness.RunResult]) -> list[str]:
    """Mean health of the changed source files (passing runs only), and the reference solution's."""
    code = [r.process["code"] for r in rows if r.score >= PASSING and r.process.get("code") is not None]
    refs = [r.process["code_ref"] for r in rows if r.process.get("code_ref") is not None]
    return [f"{statistics.mean(code):.2f}" if code else "–", f"{refs[0]:.2f}" if refs else "–", smell_cell(rows)]


def smell_cell(rows: list[harness.RunResult]) -> str:
    """Mean smells added / fixed by the change (aihealth delta), over runs that have them."""
    have = [r.process for r in rows if r.process and r.process.get("smells_added") is not None]
    if not have:
        return "–"
    added = statistics.mean(p["smells_added"] for p in have)
    return f"+{added:.1f} / -{statistics.mean(p['smells_fixed'] for p in have):.1f}"


def process_cells(rows: list[dict]) -> list[str]:
    def mean(key: str, fmt: str) -> str:
        values = [r[key] for r in rows if r.get(key) is not None]
        return fmt.format(statistics.mean(values)) if values else "–"
    strays = statistics.mean(len(r.get("strays", [])) for r in rows) if rows else 0
    first = [r["first_deliverable"] / 60 for r in rows if r.get("first_deliverable") is not None]
    return [mean("health", "{:.2f}"), mean("overhead", "{:.0%}"), mean("rewrites", "{:.1f}"),
            f"{strays:.1f}" if rows else "–", f"{statistics.mean(first):.1f} min" if first else "–"]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.report:
        report(load_results(args.report))
        return 0
    tasks = pick_tasks(args.task, args.quick)
    if args.resume and not args.out:
        raise SystemExit("--resume needs --out: the folder of the benchmark to continue")
    if args.check:
        return check(tasks)
    folder = run_all(args, tasks)
    report(load_results(folder))
    out.print(f"Runs are kept in {folder} (each run's repo, work folder and requests.jsonl, and results.jsonl).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
