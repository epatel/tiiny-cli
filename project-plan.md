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
- [x] M3 — NPU swap planner (LRU, busy-safe) + fragmentation fallback, verified live (2026-11-08)
- [x] M4 — Agentic setup: CLAUDE.md, cards, hooks, this plan (2026-11-08)
- [ ] M5 — Polish: `tai serve` (OpenAI-compatible local proxy that auto-loads models)? shell completion? OCR/rerank if models get installed

## Decisions
- 2026-11-08 — Command name `tai` (`tiiny` is taken by the official CLI). Locked.
- 2026-11-08 — Talk to the device's HTTP API directly with httpx, not by shelling out to `tiiny`. Reuse `~/.local/tiiny/config.json` for device IP + token. Locked.
- 2026-11-08 — When a model doesn't fit: ask, then unload LRU idle models (`-y` skips the prompt). Locked (user choice).
- 2026-11-08 — TTS uses `/v1/synthesize`, not `/v1/audio/speech` (the latter fails upstream for OmniVoice).
- 2026-11-08 — Chat history is persisted text-only. Images attached in a past turn are not re-sent on resume.

## Current state / handoff
All seven installed models were exercised live through `tai`: chat (Qwen3-Coder-Next, Qwen3.6 incl. vision and thinking toggle), Z-Image-Turbo, OmniVoice (incl. `instruct`), Qwen3-ASR, Qwen3-Embedding and TRELLIS 3D (shape→texture→export, and `tai 3d --faces 200000` → 16 MB GLB in ~7 min). 20 unit tests pass. After testing, the device state was left as is (the user said not to reload the coder model).

## Open questions
- Do the TRELLIS controls (`seed`, `inference_quality`) belong in `params` at submit time? `tai 3d --quality/--seed` sends them there; this is untested.
