# Benchmarks (`benchmarks/`)

These benchmarks compare `tai agent` in plain mode (one conversation) with phased work on the device. `benchmarks/README.md` is the user guide. This card covers the parts that aren't obvious.

## Shape
- **`run.py`** is the CLI. It handles `--check` (graders only), `--report DIR`, device setup (`App`, `ensure_loaded`, the same think settings as `tai agent`), the schedule and the summary table.
  - The schedule alternates the mode order between runs (plain→phased, then phased→plain), so drift on the device doesn't favour one mode.
  - Results are appended to `results.jsonl` after every run.
- **`harness.py`** has no UI or device code. It takes any client with `chat_stream`, so `tests/test_benchmarks.py` drives it with a fake model.
  - `run_task(Run, Setup)` → `RunResult`.
  - `MeteredClient` counts requests and usage tokens.
  - `Driver` runs turns, records each `TurnEnd` reason, and calls `agent.cancel()` from a timer at `Limits.timeout`.
- **Plain mode:** `agent.run(task.md)`. Up to `Limits.nudges` "Continue." messages after `max_steps`. A `Compactor` at 85, as in `tai agent`.
- **Phased mode:** `PhaseRunner.start(task.md)` with `auto = True` and `start_phases` removed.
  - Each phase runs `agent.run(runner.begin())`.
  - A phase turn that ends `done` or `max_steps` without `phase_done` gets `NUDGE_PHASE` (limited), standing in for a user typing "go on".
  - Anything other than `phase_done` ends the run, recorded as e.g. `plan (done)`.
- **Prompts and tools:** both modes get `HEADLESS` appended to the system prompt (no user, don't ask). Tools are the built-ins only: no web, MCP, media or skills.

## Safety (nobody approves tool calls)
- **bash:** commands run under `ToolContext.wrap` = `sandbox-exec` with `SANDBOX`.
  - Writes are allowed only under the run's `repo/` and `tmp/`, plus /dev. There is no network.
  - Reads of the task's parent folder are denied, which hides the hidden tests, graders and solutions.
  - `TMPDIR` points into `tmp/`, and `PATH` starts with the venv's bin, so `python` and `pytest` work.
- **File tools:** read, grep, glob and ls are wrapped (`_confined`) to refuse paths outside the repo. The approver denies write/edit whose preview is `outside`.
- **Grading** runs the agent's code too, under the same sandbox. Because the sandbox hides the tasks folder, `grade()` first copies the task (minus `repo/`) and `grading.py` into `tmp/grader/`.

## Graders
- **Output:** each `grade.py <repo>` prints one JSON line, `{"passed", "total", "notes"}`.
- **Hidden tests:** installed into the repo (`tests_hidden/` for pytest, `test_hidden/` for node) after the agent is done, replacing anything there.
- **Denominator:** `task.json` `hidden_tests` fixes it. An import or collection error would otherwise count as a single failure (`grading.expected`).
- **Checks with no tests** (writing, research): every check must count even when its input is missing. For example, a missing chapter adds 5 failed checks, so baseline and solution totals match. `--check` verifies that the totals match.
- **Reference answers:** `solution/` is copied over the repo, or `make_solution.py <repo>` is run. `--check` requires 100% for the solution and under 50% for the untouched repo.
- **aihealth:** `benchmarks/tasks/*` is excluded in `.aihealth.toml`. Fixtures have interfaces fixed by their spec (e.g. `Store.reserve`'s five parameters).

## Judge (`judge.py`, `--judge`)
- The judge is the device model scoring `task.json` `judge.files` against `judge.rubric`, 1–10 each, answering in JSON. `parse` clamps the scores and returns `{"error"}` when the answer can't be read.
- It runs outside the sandbox, after grading.
- It is the same model that did the work, so it is good for plain-vs-phased comparisons only.

## Adding a task
1. Write the files listed in `benchmarks/README.md`, with an exact spec in `task.md`.
2. Write a reference solution.
3. Set `hidden_tests` in `task.json`.
4. Run `make bench-check`.
