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
  - [x] MCP servers (Claude Code `.mcp.json` format + `~/.config/tai/mcp.json`; ask unless readOnlyHint) and skills (`.claude/skills`, `~/.claude/skills`, `~/.config/tai/skills`), plus `tai mcp` / `tai skills`
  - Later: context compaction for long sessions, /model inside the TUI, MCP over HTTP verified against a real server, MCP OAuth
- [ ] M7 — Robustness from the community API notes (cards/tiiny-api-notes.md): detect the post-restart 502 lock and suggest `tiiny login` (also after `/tiiny restart`); retry once on `xh2a backend fail` right after a load; tell users their chats are captured in the device vault, and maybe add a `/tiiny forget-day`
- [ ] M5 — Polish: `tai serve` (OpenAI-compatible local proxy that auto-loads models)? shell completion? OCR/rerank if models get installed

## Decisions
- 2026-11-08 — Command name `tai` (`tiiny` is taken by the official CLI). Locked.
- 2026-11-08 — Talk to the device's HTTP API directly with httpx, not by shelling out to `tiiny`. Reuse `~/.local/tiiny/config.json` for device IP + token. Locked.
- 2026-11-08 — When a model doesn't fit: ask, then unload idle models (`-y` skips the prompt). Locked (user choice).
- 2026-11-08 — Victims are the idle set freeing the least NPU (ties: fewer models, then least recently used), replacing pure LRU, which once unloaded the 94% coder to free 1%. User choice.
- 2026-11-08 — TTS uses `/v1/synthesize`, not `/v1/audio/speech` (the latter fails upstream for OmniVoice).
- 2026-11-08 — `tai agent` (user choices): asks before write/edit (shows diff) and bash (shows command), with y/n/always; tools = read, write, edit, bash + grep, glob, ls; full-screen TUI with textual. Tool calling verified on Qwen3-Coder-Next via standard OpenAI `tools` (streamed deltas work).
- 2026-11-08 — Image tool + viewer (user choices): built-in tools, not MCP; the viewer shows items and asks questions; the image tool asks only when it must unload models; aiohttp is a new runtime dependency.
- 2026-11-08 — MCP + skills (user choices): MCP config in Claude Code's format from the project's `.mcp.json` + `~/.config/tai/mcp.json` (not `~/.claude.json`); MCP tools ask unless marked readOnlyHint, and "always" is per server. Skills are shared with Claude Code (`.claude/skills`, `~/.claude/skills`) + `~/.config/tai/skills`, using progressive disclosure through a `skill` tool.
- 2026-11-08 — Agent core (`src/tai/agent/loop.py`) is UI-independent: it yields events and asks a permission callback, so it is tested without the TUI or a device. The TUI runs it in a worker thread.
- 2026-11-08 — Chat history is persisted text-only. Images attached in a past turn are not re-sent on resume.

## Current state / handoff
All seven installed models were exercised live through `tai`: chat (Qwen3-Coder-Next, Qwen3.6 incl. vision and thinking toggle), Z-Image-Turbo, OmniVoice (incl. `instruct`), Qwen3-ASR, Qwen3-Embedding and TRELLIS 3D (shape→texture→export, and `tai 3d --faces 200000` → 16 MB GLB in ~7 min). 147 unit tests pass (no device needed; the MCP tests start a real stdio test server). `tai agent` is built and fully tested headlessly, but has not had a live run yet: it needs the user at the keyboard. After testing, the device state was left as is (the user said not to reload the coder model).

## Open questions
- Do the TRELLIS controls (`seed`, `inference_quality`) belong in `params` at submit time? `tai 3d --quality/--seed` sends them there; this is untested.
