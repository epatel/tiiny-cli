# tai — CLI for a Tiiny device

`tai` is a Python CLI for using every model installed on a Tiiny pocket AI device (chat, vision, image generation, TTS, ASR, embeddings, image-to-3D) over Wi-Fi. It talks to the device's HTTP gateway directly. The official `tiiny` CLI only supplies the config (device IP + auth key). The device serves only *loaded* models, within a shared NPU budget, so `tai` loads models on demand and asks before unloading others.

Always read @project-plan.md before starting. It holds the shared goal and current state. Update its Current state and Decisions before you finish.

## Known stack — use what you already know

These are standard and used the standard way. There are no cards for them: read the code and apply the usual idioms.

- **click**: one group `cli` in `src/tai/cli.py`; every command takes `App` via `@pass_app` (lazy `client`, `store`). `TaiGroup.invoke` turns `ConfigError`/`TiinyError`/`ResolveError` into `error: …` + exit 1. The `tai chat` REPL is `ChatSession` in `src/tai/chat.py`: state in `SessionState`, one `cmd_*` method per `/command` registered in `self.commands`, and the model reply injected as a callable (so `tests/test_chat.py` needs no device).
- **httpx** (sync): all HTTP is in `src/tai/client.py` (`TiinyClient`). SSE is parsed by `iter_sse`. No OpenAI SDK.
- **rich**: `out` (stdout) for results, `err` (stderr) for progress and prompts, so stdout stays pipeable. Streamed chat text goes straight to `sys.stdout`.
- **sqlite3**: `src/tai/db.py`, `PRAGMA user_version` + an append-only `MIGRATIONS` list. The DB lives at `$TAI_HOME/tai.db` (default `~/.local/share/tai/`).
- **pytest + respx**: `tests/` mocks HTTP; no device is needed. Live checks are manual `tai …` runs.

## Commands

`make help` lists the targets:
- Setup: `make setup` (creates `.venv`, installs with dev extras)
- Run: `.venv/bin/tai --help` (e.g. `.venv/bin/tai models`, `.venv/bin/tai ask "hi"`)
- Test: `make test` (one test: `.venv/bin/pytest tests/test_models.py::test_swap_unloads_the_least_npu_that_makes_room`)
- Before committing: `make check` (tests + `aihealth delta`). `make scan` gives a project health overview
- Put `tai` on PATH: `make link` (symlink in `~/.local/bin`); `make clean` removes caches
- Live runs against the device: set `TAI_HOME=<scratch dir>` so test sessions stay out of the user's history. Note that a live run may unload the user's models.

Enforced by hooks (`.claude/settings.json`): `pip install` outside `.venv/bin/` is blocked, so use `.venv/bin/pip`. Finishing with uncommitted changes in `src/` or `tests/` runs the unit tests and blocks on failure. Reading `.env` is denied.

## Code health
- aihealth is pinned in the `dev` extras (`.venv/bin/pip install -e '.[dev]'` installs it). Hooks and the `aihealth` MCP server (`.mcp.json`) use `.venv/bin/aihealth` and do nothing if it isn't installed.
- Before committing, run `make health` (`.venv/bin/aihealth delta`) and fix new or worsened smells it reports in code you touched (exit code 1 = health degraded).
- `.venv/bin/aihealth scan .` gives an overview of the codebase.

## Cards

### Domains
- [device-api](cards/device-api.md) — calling or debugging any device endpoint, adding a new model type/task, or an HTTP error from the device
- [npu-swapping](cards/npu-swapping.md) — anything about loading/unloading models, NPU budget, "Cannot allocate memory", or which model is picked by default

### Decisions
- [standing-defaults](cards/standing-defaults.md) — before installing a dependency or changing the sqlite schema (venv only; add a migration, never edit one)

When you commit a change that adds non-obvious logic, settles an architectural choice, or makes a card above wrong, update or add the card in that commit and keep this index in sync. Proposed diffs and answers need no card edits. Cards stay self-contained: no card links to another.
