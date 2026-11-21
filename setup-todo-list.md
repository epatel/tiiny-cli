---
setup-format: 1
project: tai
---

# Project setup

> **Agents:** set this project up by working through the steps below in order.
> Record progress in `setup-todo-list.local.md` (gitignored), one line per step:
> `- <id>: done @rev<N> <date>`, or `skipped` / `failed` with a reason.
> Respect each step's `gate`: `auto` = just run it, `approve` = show the commands
> and wait for a yes, `input` = ask for the listed values first. Run a step's
> `check` first; if it passes, the step is already done (for `input` steps,
> still confirm the value). Never write secrets into either file. To re-check
> later ("doctor"), compare the progress lines with this file by id and `rev`,
> and re-run the checks.
>
> **Humans:** every step below is plain instructions. Follow them by hand if you prefer.

## [python-311] Install Python 3.11 or newer
- required: yes
- gate: approve
- rev: 1
- check: `for p in python3 python3.13 python3.12 python3.11; do command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' && exit 0; done; exit 1`

tai itself runs on Python 3.10, but the dev tools that `make setup` installs (aihealth, pinned in the `dev` extras) need 3.11 or newer. CI tests 3.10 without them. macOS ships 3.9 (`/usr/bin/python3`), so install a newer one: `brew install python@3.12` or `uv python install 3.12`.

## [git] Git 2.24 or newer
- required: yes
- gate: approve
- rev: 1
- check: `git --version | awk '{split($3, v, "."); exit !(v[1] > 2 || (v[1] == 2 && v[2] >= 24))}'`

`aihealth delta` (part of `make check` and the editor hooks) compares the working tree with git. On macOS, `xcode-select --install` or `brew install git`.

## [venv] Create .venv and install tai with the dev tools
- required: yes
- gate: approve
- rev: 1
- depends: python-311
- check: `.venv/bin/tai --help >/dev/null 2>&1 && test -x .venv/bin/pytest && test -x .venv/bin/aihealth`

Run `make setup`. If `python3` on your PATH is older than 3.11, name a newer one: `make setup PYTHON=python3.12`. It creates `.venv` and installs tai in editable mode with pytest, respx and aihealth (aihealth comes from GitHub, pinned to a commit). Always install into the venv (`.venv/bin/pip`); a Claude Code hook blocks other `pip install`s.

## [device-access] Give tai the device address and API key
- required: no
- gate: input
- rev: 1
- inputs: TAI_DEVICE — "the Tiiny's IP address on your Wi-Fi", TIINY_API_KEY (secret) — "API key from the TiinyOS app (or use tiiny login instead)"
- check: `test -s "$HOME/.local/tiiny/config.json" || grep -qs '^TIINY_API_KEY=.' "$HOME/.config/tai/.env"`

Only needed to run tai against a device; the unit tests mock it. Either:
- with the official `tiiny` CLI: `tiiny connect <device-ip>` and `tiiny login` (tai reads `~/.local/tiiny/config.json`), or
- write `TAI_DEVICE=<ip>` and `TIINY_API_KEY=<key>` to `~/.config/tai/.env` (create the folder; `chmod 600` the file).

A `.env` in the project folder overrides both; an old key left there gives `HTTP 401 … auth_failed`. Check with `.venv/bin/tai status`.

## [link] Put tai on PATH
- required: no
- gate: approve
- rev: 1
- depends: venv
- check: `test "$(readlink "$HOME/.local/bin/tai")" = "$PWD/.venv/bin/tai"`

Run `make link` (a symlink in `~/.local/bin`; `PREFIX=...` for another place, `make unlink` to remove). `~/.local/bin` must be on your PATH.

## [jq] Install jq for the Claude Code hooks
- required: no
- gate: approve
- rev: 1
- check: `command -v jq >/dev/null`

The hooks in `.claude/hooks/` read their input with jq. Without it the venv-only pip guard does nothing (it fails open). On macOS: `brew install jq`.
