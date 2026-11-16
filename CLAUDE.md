# tai — CLI for a Tiiny device

`tai` is a Python CLI for using every model installed on a Tiiny pocket AI device (chat, vision, image generation, TTS, ASR, embeddings, image-to-3D) over Wi-Fi. It talks to the device's HTTP gateway directly. The official `tiiny` CLI only supplies the config (device IP + auth key). The device serves only *loaded* models, within a shared NPU budget, so `tai` loads models on demand and asks before unloading others.

Always read @project-plan.md before starting. It holds the shared goal and current state. Update its Current state and Decisions before you finish.

## Known stack — use what you already know

These are standard and used the standard way. There are no cards for them: read the code and apply the usual idioms.

- **click**: one group `cli` in `src/tai/cli.py`; every command takes `App` via `@pass_app` (lazy `client`, `store`). `TaiGroup.invoke` turns `ConfigError`/`TiinyError`/`ResolveError` into `error: …` + exit 1. The `tai chat` REPL is `ChatSession` in `src/tai/chat.py`: state in `SessionState`, one `cmd_*` method per `/command` registered in `self.commands`, and the model reply injected as a callable (so `tests/test_chat.py` needs no device).
- **httpx** (sync): all HTTP is in `src/tai/client.py` (`TiinyClient`). SSE is parsed by `iter_sse`. No OpenAI SDK.
- **rich**: `out` (stdout) for results, `err` (stderr) for progress and prompts, so stdout stays pipeable. Streamed chat text goes straight to `sys.stdout`.
- **sqlite3**: `src/tai/db.py`, `PRAGMA user_version` + an append-only `MIGRATIONS` list. The DB lives at `$TAI_HOME/tai.db` (default `~/.local/share/tai/`).
- **textual** (only `tai agent`): `src/tai/agent/tui.py`. Pilot tests in `tests/test_agent_tui.py`.
- **pytest + respx**: `tests/` mocks HTTP; no device is needed. Live checks are manual `tai …` runs.

## Commands

`make help` lists the targets:
- Setup: `make setup` (creates `.venv`, installs with dev extras)
- Live device monitor: `.venv/bin/tai top` (`src/tai/top.py`, curses; `render` is pure and tested in `tests/test_top.py`)
- Run: `.venv/bin/tai --help` (e.g. `.venv/bin/tai models`, `.venv/bin/tai ask "hi"`, `.venv/bin/tai agent` in a project folder)
- Test: `make test` (one test: `.venv/bin/pytest tests/test_models.py::test_swap_unloads_the_least_npu_that_makes_room`)
- Before committing: `make check` (tests + `aihealth delta`). `make scan` gives a project health overview
- Benchmarks: `make bench-check` (graders, no device); `make bench` runs plain vs phased on the device (slow, the user runs it)
- Put `tai` on PATH: `make link` (symlink in `~/.local/bin`); `make clean` removes caches
- Live runs against the device: set `TAI_HOME=<scratch dir>` so test sessions stay out of the user's history. Note that a live run may unload the user's models.

Enforced by hooks (`.claude/settings.json`): `pip install` outside `.venv/bin/` is blocked, so use `.venv/bin/pip`. Finishing with uncommitted changes in `src/` or `tests/` runs the unit tests and blocks on failure. Reading `.env` is denied.

## Code health
- aihealth is pinned in the `dev` extras (`.venv/bin/pip install -e '.[dev]'` installs it). Hooks and the `aihealth` MCP server (`.mcp.json`) use `.venv/bin/aihealth` and do nothing if it isn't installed.
- Fix new or worsened smells that `make health` (`.venv/bin/aihealth delta`, part of `make check`) reports in code you touched (exit code 1 = health degraded).

## Cards

### Domains
- [device-api](cards/device-api.md) — calling or debugging any device endpoint, adding a new model type/task, or an HTTP error from the device
- [agent](cards/agent.md) — anything in `src/tai/agent/` (`tai agent`): the loop, permissions, the TUI, `/tiiny` device commands, the model and thinking, agent sessions, a model swapped out by a tool, how agent tests are written
- [agent-tools](cards/agent-tools.md) — the agent's MCP servers (`tai mcp`), skills (`tai skills`), browser viewer + image/video/show/ask tools, or web_search/web_fetch
- [agent-context](cards/agent-context.md) — the agent's context limits: phased work (`/phase`, `.tai/work/`), compaction (`/compact`, `agent.compact`), or the context budget
- [benchmarks](cards/benchmarks.md) — `benchmarks/` (plain vs phased runs on the device): the harness, sandbox, graders, adding a task
- [tiiny-api-notes](cards/tiiny-api-notes.md) — a device feature tai doesn't use yet (vault/kb, OCR, rerank, music, video, Jev classification, image editing), image seed/steps/sizes or TRELLIS stage params (from Tiiny Studio's code), odd device behaviour (502 after a restart, 504 after ~10 min, `xh2a backend fail`), or privacy of chats on the device
- [serve](cards/serve.md) — `tai serve`, client mode (`--url`/`TAI_URL` + `TAI_API_KEY`), nginx prefix, forwarding/streaming/auth
- [npu-swapping](cards/npu-swapping.md) — anything about loading/unloading models (`src/tai/loader.py`), NPU budget, "Cannot allocate memory", or which model is picked by default

### Decisions
- [standing-defaults](cards/standing-defaults.md) — before installing a dependency or changing the sqlite schema (venv only; add a migration, never edit one)

When you commit a change that adds non-obvious logic, settles an architectural choice, or makes a card above wrong, update or add the card in that commit and keep this index in sync. Proposed diffs and answers need no card edits. Cards stay self-contained: no card links to another.
