# Project Plan: tai — use every Tiiny model from the terminal

## Goal
A Python CLI (`tai`) that makes every model installed on the user's Tiiny device usable over Wi-Fi with one command per task. Model loading and NPU swapping happen transparently, asking before anything is unloaded.

## Non-goals
- Replacing the official `tiiny` CLI for device admin (activation, login, Wi-Fi, firmware, downloads, knowledge-base vault, connectors).
- USB-C transport.
- Running models locally or proxying to cloud providers.

## Milestones
- [x] M1 — API discovery: gateway, auth, management + inference endpoints for all 7 installed models (2026-11-08)
- [x] M2 — CLI: models/status/load/unload, ask, chat (sessions in sqlite), image, tts, asr, embed, 3d, config (2026-11-08)
- [x] M3 — NPU swap planner (least-NPU-freed, busy-safe) + fragmentation fallback, verified live (2026-11-08)
- [x] M4 — Agentic setup: CLAUDE.md, cards, hooks, this plan (2026-11-08)
- [ ] M6 — `tai agent`: coding agent in a full-screen TUI (2026-11-08)
  - [x] tools (read, write, edit, bash, grep, glob, ls) with permission previews
  - [x] agent loop (streamed tool calls, UI-independent, permission callback)
  - [x] sessions persisted (migration 3: session kind/cwd, full message JSON)
  - [x] textual TUI (log, input, permission modal, interrupt) + pilot tests
  - [ ] live run on the device, by the user (scripted auto-approval is blocked by the harness)
  - [x] Browser viewer (aiohttp + WebSocket, token + origin check) with built-in `image`, `show` and `ask` tools; checked live in Chrome
  - [x] Agent model swapped out by a tool (e.g. `tai image` via bash) is reloaded automatically after asking; status line shows activity + elapsed time
  - [x] `/tiiny` in the TUI: status, models, load/unload, restart/shutdown (loader split into `loader.py` with a CLI and a TUI `LoaderUI`)
  - [x] Multi-line prompt (`PromptArea`): Enter sends; Ctrl+J / Alt+Enter / `\`+Enter add a newline; pastes keep newlines
  - [x] Prompt history: ↑/↓ recall earlier prompts (per project folder, sqlite migration 4)
  - [x] `video` tool (MiniMax-H3-Turbo via `tai.video`, Esc stops waiting); not yet run live inside the TUI
  - [x] MCP servers (Claude Code `.mcp.json` format + `~/.config/tai/mcp.json`; ask unless readOnlyHint) and skills (`.claude/skills`, `~/.claude/skills`, `~/.config/tai/skills`), plus `tai mcp` / `tai skills`
  - [x] Context compaction (2026-11-14): `/compact [focus]` + automatic at `agent.compact` % (default 85); not run live yet
  - Later: /model inside the TUI, MCP over HTTP verified against a real server, MCP OAuth
- [ ] M7 — Robustness from the community API notes (cards/tiiny-api-notes.md): detect the post-restart 502 lock and suggest `tiiny login` (also after `/tiiny restart`); retry once on `xh2a backend fail` right after a load; tell users their chats are captured in the device vault, and maybe add a `/tiiny forget-day`
- [x] M8 — `tai serve` + client mode (2026-11-08): one program, three modes (standalone / `tai serve` / client via `--url`/`TAI_URL` + `TAI_API_KEY`); forwards `/v1/*` + `/api/v1/*` with key swap and streaming; checked live through a `/tai` prefix (status, embed, streamed chat). Later: launchd/systemd example, config polish.
- [ ] M9 — Phased work in `tai agent` (2026-11-14): `/phase start <goal>` runs explore → plan → the phases the plan defines, each in a fresh context fed from `.tai/work/<task>/` (plan.md, notes, tools, tests) and the earlier handoffs
  - [x] `agent/phases.py`: work folder + `state.json`, `PhaseRunner`, `phase_done` tool (ends the turn), phase prompts
  - [x] loop: context budget (wrap-up nudge at 80%, stop at 95%), `end_turn`, `auto_allow` (writes inside the work folder don't ask)
  - [x] TUI: `/phase start|status|next|redo|resume|stop`, gate y/a/n between phases, phase in the title; one saved session per phase
  - [x] tests (loop + runner with the fake stream, TUI pilot), card
  - [ ] live run by the user
- [ ] M5 — Polish: shell completion? OCR/rerank if models get installed?

## Decisions
One line each; the how and why live in the cards and the code.
- 2026-11-08 — Command name `tai` (`tiiny` is taken). Locked.
- 2026-11-08 — Talk to the device's HTTP API with httpx, not via the `tiiny` CLI; reuse its `~/.local/tiiny/config.json`. Locked.
- 2026-11-08 — A model that doesn't fit: ask, then unload idle models (`-y` skips). Locked (user choice).
- 2026-11-08 — Unload the idle set that frees the least NPU, not LRU (LRU once unloaded the 94% coder to free 1%). User choice.
- 2026-11-08 — TTS uses `/v1/synthesize`; `/v1/audio/speech` fails for OmniVoice.
- 2026-11-08 — `tai agent`: textual TUI; asks before write/edit/bash (y/a/n); standard OpenAI `tools`. User choices.
- 2026-11-08 — Agent loop is UI-independent (events + permission callback), so it's tested without TUI or device.
- 2026-11-08 — Viewer + image tool are built in, not MCP; aiohttp added. User choices.
- 2026-11-08 — MCP in Claude Code's `.mcp.json` format (+ `~/.config/tai/mcp.json`, not `~/.claude.json`); skills shared with Claude Code. User choices.
- 2026-11-08 — `tai image` always sends and prints a seed (the device doesn't return it); `tai 3d` filters params by `stage_specs`.
- 2026-11-08 — Agent prompt history per project folder in sqlite, not a dotfile.
- 2026-11-08 — `tai chat` can hide reasoning (`/think hide`, `show_thinking`): display only.
- 2026-11-08 — `--no-save` uses an in-memory store; `tai history clear` deletes and VACUUMs. Neither touches the device's vault.
- 2026-11-08 — Chat history is saved text-only: images from past turns aren't re-sent on resume.
- 2026-11-08 — `tai serve`: one personal key, binds 127.0.0.1, nginx owns the URL, a forwarding proxy so clients don't change. User choices.
- 2026-11-09 — Python ≥ 3.10 (aiohttp, click, mcp); macOS ships 3.9, so `make setup` checks and takes `PYTHON=`, and code avoids 3.11-only builtins.
- 2026-11-09 — `tai info MODEL`: catalog + what the model reports; asks before loading just to query. User choice.
- 2026-11-09 — `-x KEY=VALUE` for device options without a flag (`-x`, not `--x:key`: click can't declare dynamic option names); no validation. User idea.
- 2026-11-09 — `tai video`: options checked against `/v1/video/info` before submitting (the device accepts impossible ones and fails later). User request.
- 2026-11-09 — `agent.think` separate from `think`: reasoning can push an agent step past the 10-minute limit. User request.
- 2026-11-09 — CI: Python 3.10–3.13 + macOS, aihealth gate ≥ 9 (not on 3.10), pip-audit, CodeQL, Scorecard, Dependabot.
- 2026-11-11 — `tai --version` asks git at run time, not a pre-commit hook (hooks aren't shared, can't know their own commit, and would rewrite a tracked file). User request.
- 2026-11-11 — Agent web tools: DuckDuckGo HTML search (no key, fragile) + page fetch; both ask. User choices.
- 2026-11-14 — Setup refreshed to ai.memention.net 2026-11-14.4 (aihealth pinned to a commit, `migrate()` refuses newer databases); feature-first/two-tier declined.
- 2026-11-14 — Phased work: explore + plan fixed, the plan defines the rest; y/a/n between phases; work folder in the project; TUI only. User idea, choices as suggested.
- 2026-11-14 — Compaction: summary + latest messages, manual and automatic at 85%, off in phases. `compact.py` keeps aihealth's Primitive Obsession (messages are plain dicts throughout tai).

## Current state / handoff
- **Built:** every command in M2 plus `info`, `video`, `-x`, `serve`/client mode, and `tai agent` with tools, viewer, media, web, MCP, skills, `/tiiny`, phased work (M9) and compaction. 249 unit tests pass (no device needed), also in CI on Python 3.10–3.13 + macOS.
- **Checked live:** all seven original models, plus `tai info`, `tai video` and `-x` (2026-11-08/09). Results and timings are in the cards (device-api, agent). The user has used `tai agent` (the 2026-11-09 thinking-label report), but the M6 live-run item is still open.
- **Not run live yet:** phased work, compaction, and the `video` tool inside the TUI.
- **Device, last seen 2026-11-09:** only Qwen3.8-27B loaded. Don't reload the coder model unasked; a live run may unload the user's models.
- **Next:** a live agent run (`/phase start …`, `/compact`); then M7 robustness or M5 polish.

## Open questions
- None right now.
