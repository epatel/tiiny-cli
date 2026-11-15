# Benchmarks (`benchmarks/`)

These benchmarks compare `tai agent` in plain mode (one conversation) with phased work on the device. `benchmarks/README.md` is the user guide. This card covers the parts that aren't obvious.

## Shape
- **`run.py`** is the CLI. It handles `--check` (graders only), `--report DIR`, device setup (`App`, `ensure_loaded`, the same think settings as `tai agent`), the schedule and the summary tables. `-m` is repeatable: models run one after another (each loaded once), and each result carries `model`. Rows from before that field take it from the old `meta.json` `model`.
  - The schedule alternates the mode order between runs (plain→phased, then phased→plain), so drift on the device doesn't favour one mode.
  - Results are appended to `results.jsonl` after every run.
- **`harness.py`** has no UI or device code. It takes any client with `chat_stream`, so `tests/test_benchmarks.py` drives it with a fake model.
  - `run_task(Run, Setup)` → `RunResult`.
- **`transcript.py`**: `MeteredClient` wraps the client. It counts requests and usage tokens, totals the written tokens per reply kind (`output_by`: the first tool called, or `text`), the writing speed from llama.cpp `timings` (`predicted_n` / `predicted_ms`), and appends each request to `<run>/requests.jsonl`.
  - A request logs only the messages added since the previous one. When the new messages don't extend the previous ones (the first request, a new phase, a compaction), it logs all of them with `fresh: true`.
  - The record is written in a `finally`, so a request cancelled at the time limit is logged too.
  - `Driver` runs turns, records each `TurnEnd` reason, and calls `agent.cancel()` from a timer at `Limits.timeout` (0 = no timer). A run cut off by it is still graded but gets `timed_out: true`; the run log and the summary's `timed out` column show it, because its score is for unfinished work. `RunResult.from_json` reads older rows without the field as timed out when their last turn was `interrupted`.
  - **Device errors:** the device sometimes breaks a reply (cut off without `[DONE]`, then empty replies for seconds, or an `Upstream chat stream failed` chunk). `TiinyClient.chat_stream` raises `TiinyError` for both, so the turn ends `error` instead of looking done. `MeteredClient.broken` counts requests without usage → `RunResult.device_errors`; `errored` (not when timed out) makes `run_one` move the folder to `<run>-error[-n]` (`keep_broken`), wait `RETRY_WAIT` s and run once more. The report leaves errored runs out of every table and lists them below.
  - **Silent drops:** the device can also stop sending mid-reply with the TCP connection open. `chat_stream` reads with an idle timeout (`STREAM_IDLE_PROGRESS` 180 s when `return_progress` keep-alives are on, else `STREAM_IDLE` 600 s), so such a request raises instead of hanging the run (one hung 25 min before this).
  - **`--resume`** (needs `--out`): `finished(folder)` reads `results.jsonl`; runs already there and not errored are skipped, so a killed run continues where it stopped.
- **Variants (`variants.py`, `variants/*.json`):** a `Run` carries a `Variant` (mode plain/checklist/phased, a system prompt addition, thinking per stage, `stop_after`); `Run.mode` is its name, so results group by variant. `Driver.stage` swaps `agent.config.extra` per stage from the extras at start. `--mode` maps to the `plain`/`phased` variants; `-V` replaces it.
- **Checklist mode:** the task + `CHECKLIST`, then one `REVIEW` turn; both get the step-limit nudges. Compacts like plain.
- **Process metrics (`process.py`):** computed from `requests.jsonl` and the repo after the run, *before* grading installs hidden tests. Deliverables are fnmatch globs from `task.json` (`*` crosses `/`); `strays` compares against the task's starting repo and ignores `.tai/`, caches and `tests_hidden`/`test_hidden`. `health` runs `<venv>/bin/aihealth scan --format json` with a temp config excluding `.tai/` and `node_modules/`; it only parses code, so it runs outside the sandbox.
  - **Code grade (`code`, `code_ref`):** the project `health` mixes in the starting code and the agent's own tests (which score ~10, so more tests looked healthier). `code` is the line-weighted aihealth score of only the non-test files that differ from the starting repo (`TEST_FILE` pattern); `code_ref` is the same for `solution/` laid over the repo (cached per task). The report's `code` column averages only runs scoring ≥ `PASSING` (0.8): an untouched repo scores well, so code health means nothing without the tests.
- **Quick suite:** `task.json` `"suite": "quick"`; `all_tasks(suite)`, `--quick`. `-t` can name a task of either suite.
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
