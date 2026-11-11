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
  - Later: context compaction for long sessions, /model inside the TUI, MCP over HTTP verified against a real server, MCP OAuth
- [ ] M7 — Robustness from the community API notes (cards/tiiny-api-notes.md): detect the post-restart 502 lock and suggest `tiiny login` (also after `/tiiny restart`); retry once on `xh2a backend fail` right after a load; tell users their chats are captured in the device vault, and maybe add a `/tiiny forget-day`
- [x] M8 — `tai serve` + client mode (2026-11-08): one program, three modes (standalone / `tai serve` / client via `--url`/`TAI_URL` + `TAI_API_KEY`); forwards `/v1/*` + `/api/v1/*` with key swap and streaming; checked live through a `/tai` prefix (status, embed, streamed chat). Later: launchd/systemd example, config polish.
- [ ] M5 — Polish: `tai serve` (OpenAI-compatible local proxy that auto-loads models)? shell completion? OCR/rerank if models get installed?

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
- 2026-11-08 — `tai image` always sends a seed (random unless `--seed`) and prints it, because the device doesn't return it; `--steps/--guidance/--negative` are sent only when given. `tai 3d` filters its controls by each stage's `stage_specs` (both from Tiiny Studio's code).
- 2026-11-08 — Agent prompt history is kept per project folder (like agent sessions) in sqlite, not a dotfile; `/commands` are included.
- 2026-11-08 — `tai chat` can hide the reasoning text (`/think show|hide`, setting `show_thinking`): display only, the model still thinks; hidden shows a stderr spinner (so does `tai ask` without `--show-thinking`).
- 2026-11-08 — `--no-save` (chat, agent) swaps in an in-memory copy of the store with only the settings, so nothing reaches disk; it can't be combined with -c/-r. `tai history clear` deletes all sessions + agent prompt history and VACUUMs. Neither affects what the device's vault records.
- 2026-11-08 — `tai serve` (user choices): one key for personal use (`TAI_API_KEY`, same name on server and client); everything tai can do locally is allowed remotely; the server only takes a port (`--port`/`TAI_SERVE_PORT`) and binds 127.0.0.1; nginx owns the external URL, and any prefix it leaves is ignored. A forwarding proxy rather than a higher-level API, so the client code is unchanged.
- 2026-11-09 — Python ≥ 3.10 (aiohttp, click and mcp need it). macOS ships 3.9, so `make setup` checks the version and takes `PYTHON=`; code avoids 3.11-only builtins (`BaseExceptionGroup` is duck-typed). `mcp` (2.x) and `httpx2` are declared runtime dependencies; they were missing, so fresh installs had no MCP.
- 2026-11-09 — `tai info MODEL` (user choice): catalog row + whatever the model reports (`/v1/capabilities` for chat/TTS, `/v1/3d/capabilities` for 3D); asks before loading a model just to query it. Image/ASR/embedding report nothing.
- 2026-11-09 — `-x KEY=VALUE` on ask/chat/image/tts/asr/embed for device options without a flag (user idea; `-x` instead of the suggested `--x:language` because click can't declare dynamic option names). Placement per command (TTS: inside `config`), dotted keys nest, JSON values, deep-merged over tai's own fields. No validation: the device decides. Not on `tai 3d` (per-stage params).
- 2026-11-09 — `tai video` for MiniMax-H3-Turbo (user request): one OpenAI-style job (submit → poll → MP4), options checked against `/v1/video/info` before submitting because the device accepts impossible sizes/lengths and fails later; seed always sent and printed, like `tai image`; `-i` start frame as a data URL. Built from the community notes, then checked live.
- 2026-11-09 — `agent.think` setting + `tai agent --think/--no-think` (user request): thinking for the agent apart from chat, because reasoning can push an agent step past the 10-minute request limit. Prompted by a Qwen3-Coder-Next vs Qwen3.8-27B comparison (cards/agent.md, Model).
- 2026-11-09 — CI on GitHub Actions (Python 3.10–3.13 + macOS, aihealth gate ≥ 9 except on 3.10, where aihealth can't install), daily pip-audit, CodeQL, OpenSSF Scorecard, Dependabot; same setup as ai-health.
- 2026-11-08 — Chat history is persisted text-only. Images attached in a past turn are not re-sent on resume.

## Current state / handoff
All seven installed models were exercised live through `tai`: chat (Qwen3-Coder-Next, Qwen3.6 incl. vision and thinking toggle), Z-Image-Turbo, OmniVoice (incl. `instruct`), Qwen3-ASR, Qwen3-Embedding and TRELLIS 3D (shape→texture→export, and `tai 3d --faces 200000` → 16 MB GLB in ~7 min). 203 unit tests pass (no device needed; the MCP tests start a real stdio test server), also in GitHub Actions CI on Python 3.10–3.13 + macOS. `tai agent` is built and fully tested headlessly, but has not had a live run yet: it needs the user at the keyboard. Verified live 2026-11-08: TRELLIS `stage_specs` lists `seed` + `inference_quality` for shape and texture, so `tai 3d --quality fast --seed 1` sends both (8m22s: shape 2:30, texture 1:14, export 4:34 at 0% NPU); `tai image --seed` reproduces byte-identical PNGs. The agent's `image` tool takes seed/steps/guidance_scale/negative_prompt too, and the system prompt tells it to raise the bash timeout for `tai 3d`. After testing, the device state was left as is (the user said not to reload the coder model). 2026-11-09: `tai info` checked live on loaded models (Qwen3.6, TRELLIS, Z-Image, embedding) and `--no-load` on the coder; the device now also lists `lightx2v/MiniMax-H3-Turbo` (Text-to-Video), which tai doesn't support yet. `tai tts -x language=sv` checked live (same audio as the raw API call). `tai video --seconds 2 --steps 4 --seed 42` rendered live in 7m17s: 2.37 s MP4, 832x480 H.264 at 24 fps + 32 kHz stereo AAC, on-prompt picture. Loading it unloaded Z-Image and TRELLIS (user OK'd); the video model was left loaded. Later that day Qwen3.8-27B was downloaded and compared with Qwen3-Coder-Next (equal on three graded tasks without thinking, half the NPU; too slow with thinking); afterwards only Qwen3.8-27B was loaded. A report that `agent.think off` was ignored turned out to be the TUI status label (always "thinking…"); the device does honour it, so the label now says "working…" unless reasoning actually streams, and the title shows the thinking mode. 2026-11-11: the agent status line shows context use from the device's `usage` chunk against the model's `context_window` (fixed per model, not settable); `tai models` and `tai info` show the window too.

## Open questions
- None right now.
