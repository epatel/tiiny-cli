# Project Plan: tai — use every Tiiny model from the terminal

## Goal
A Python CLI (`tai`) that makes every model installed on the user's Tiiny device usable over Wi-Fi with one command per task. Model loading and NPU swapping happen transparently, asking before anything is unloaded.

## Non-goals
- Replacing the official `tiiny` CLI for device admin (activation, login, Wi-Fi, firmware, knowledge-base vault, connectors). Downloads were here until 2026-11-16.
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
  - [x] breakdown entry points: `tai agent --phase TASK`, and a `start_phases` tool the agent can call for a big task (asks first; the conversation becomes the background)
  - [ ] live run by the user
- [ ] M10 — Benchmark plain vs phased (2026-11-14): `benchmarks/run.py`, headless agent loop, sandboxed bash, four tasks (Python, TypeScript, research, writing) with hidden tests or checks + optional model judge
  - [x] harness, graders with reference solutions (`--check`), tests with the fake model
  - [ ] first device run by the user (`make bench`, then `-n 3`)
- [x] `tai top` (2026-11-14; a separate `tai-top` program until 2026-11-16): live full-screen monitor of the device (NPU/CPU graphs, memory, NPU memory, budget, per-core bars, loaded models); Unicode block characters, `--ascii` (or a non-UTF-8 locale) for plain ASCII. User request.
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
- 2026-11-14 — Phased work can start from the shell (`--phase`) or be offered by the agent (`start_phases`, asks first). User request.
- 2026-11-14 — Agent step limit 50 → 200, `agent.max_steps` (or off), none in phased work: the context budget ends a phase. User report (stopped too early).
- 2026-11-14 — Benchmarks: a standalone `benchmarks/run.py`, not a tai command; bash under macOS sandbox-exec; Python, TypeScript, research and writing tasks. User choices; task fixtures excluded from aihealth (`.aihealth.toml`).
- 2026-11-14 — Compaction: summary + latest messages, manual and automatic at 85%, off in phases. `compact.py` keeps aihealth's Primitive Obsession (messages are plain dicts throughout tai).
- 2026-11-17 — Quality of small steps measured by a Claude review: `benchmarks/review.py` (Claude Agent SDK on the Claude Code login, no API key), blind pairwise A/B on five criteria; new quick quality tasks `q-py-bulk` (fit a pattern) and `q-py-pagefix` (minimal fix + regression test). User idea (quality should show in quick tests; Claude as evaluator, on the subscription).
- 2026-11-17 — Loop nudges (`agent/nudges.py`, on by default): "continue" after a reply that only announces work, and a note quoting the task on the 4th failing run in a row of the same command. Loop mechanics instead of prompt lines, which measured nothing. Not yet measured (variant `no-nudges`). User go-ahead.
- 2026-11-17 — Code Mode not built: as a prompt (`scripted` variant) Turbo didn't batch tool work into scripts; the faster runs came from fewer test runs. A 5-run confirmation showed no reliable gain and a worse tail, so nothing goes into `STYLE`. User request (investigate).
- 2026-11-16 — `tai-top` is now `tai top`, one program instead of two (same options; global `--device`/`--url` now come before `top`). User request.
- 2026-11-16 — `tai tts --clone FILE [--clone-text WORDS]` (`ref_audio`/`ref_text`), checked live with Qwen3-VoiceClone-1.7B and VibeVoice-1.5B; OmniVoice is refused by the device although its catalog row says Voice Clone. Model names now match by substring too (`tai load voiceclone`). User request.
- 2026-11-16 — `tai download [MODEL]`: downloads store models through `/api/v1/models/{id}/download/stream` (found in the official CLI's binary, checked live with Kokoro-82M); no MODEL lists the store. Downloads are no longer a non-goal. User request.
- 2026-11-16 — `default.agent` = Qwen3.6-35B-A3B-Turbo in the user's settings (2–3× faster writing than Qwen3.8-27B, equal or better benchmark scores). User choice.
- 2026-11-16 — tai agent's system prompt includes lean-shape's rules (`STYLE`): best over six runs; idioms, a finishing check and a generic quality line showed no gain. Benchmarks: `plain` is now the agent as shipped, `bare` the old baseline (`style: false`). User go-ahead.
- 2026-11-16 — Chat streams read with an idle timeout (180 s with progress keep-alives, else 600 s); benchmarks `--resume` skips finished runs. Found in the shape run (a silent drop hung it 25 min).
- 2026-11-16 — Next experiment: code-shape rules and per-language idioms as prompt variants (`lean-shape`, `lean-idioms`; variants can `extend` and add `prompt_by_domain`), not yet in tai agent. User idea (idioms per language).
- 2026-11-16 — Benchmarks: a second grade from aihealth, `code` = health of the changed non-test source files next to the reference solution's (`ref`), shown for passing runs only. User request (an alternative grade to the tests).
- 2026-11-16 — A chat stream without `[DONE]` or with an error chunk is a `TiinyError` (was silently a short reply); benchmark runs the device broke are retried once and left out of scores. Found in the quick run.
- 2026-11-16 — Quick benchmark suite + variants + process metrics for iterating on how the agent works; phase prompts fixed from what the transcripts showed. User request (quicker test to iterate on).
- 2026-11-15 — Benchmarks keep a transcript per run (`requests.jsonl`), split written tokens by reply kind, report tok/s, and compare models with repeated `-m`. User choice (steps 1 and 2 of the assessment: see where the time goes, try a faster model).
- 2026-11-15 — Benchmarks: runs cut off by `--timeout` are marked `timed_out` (log + summary column); `--timeout 0` = no limit. The default stays 60 min. User report (3 of 7 first-run results were cut off, phased hit hardest).

## Current state / handoff
- **Built:** every command in M2 plus `info`, `video`, `-x`, `serve`/client mode, and `tai agent` with tools, viewer, media, web, MCP, skills, `/tiiny`, phased work (M9) and compaction. 329 unit tests pass (no device needed), also in CI on Python 3.10–3.13 + macOS.
- **Checked live:** all seven original models, plus `tai info`, `tai video` and `-x` (2026-11-08/09). Results and timings are in the cards (device-api, agent). The user has used `tai agent` (the 2026-11-09 thinking-label report), but the M6 live-run item is still open.
- **Not run live yet:** phased work, compaction, and the `video` tool inside the TUI.
- **Device, 2026-11-15:** Qwen3.6-35B-A3B-Turbo and Qwen3.8-27B loaded (98%); `default.agent` is Qwen3.6-35B-A3B-Turbo (set 2026-11-16). Tool-capable chat models: those two, Qwen3.6-35B-A3B, Qwen3.8-27B-heretic-ara-Turbo (59%), Qwen3-Coder-Next (94%, 131K). Writing speed, thinking off, short prompt: Qwen3.6-35B-A3B-Turbo 47 tok/s, Qwen3.8-27B 19 tok/s. Don't reload the coder model unasked; a live run may unload the user's models.
- **First bench run (2026-11-14, `~/.local/share/tai/bench/20261114-200255`, full write-up in its `ASSESSMENT.md`, archived as `~/.local/share/tai/bench-archive/20261114-200255-qwen3.8-plain-vs-phased-60min.tar.gz`):** python-inventory and research-rail 100% in both modes (phased 3–10× slower); ts-templates (plain 77%, phased 18%) and writing-story (plain 68%, phased 3%) hit the 60-min limit, so those scores aren't comparable. writing-story phased spent ~26 min on explore + a 38 KB plan.md, then the rest building a story checker tool, and wrote no chapters.
- **Assessment (2026-11-15):** writing (~15 tok/s) is the bottleneck, not reading (prompt cache). Phased spent its output on scaffolding (38 KB plan, a checker tool) and its context resets bought nothing: plain never got near the 262K window. Ideas not yet done: deliverable-first/edit-over-rewrite guidance in the system prompt; a short checklist + review pass instead of phases, with phases only when the context is nearly full.
- **Second bench run (2026-11-15, Qwen3.6-35B-A3B-Turbo, no limit, `~/.local/share/tai/bench/20261115-015904`), partial:** 31–42 tok/s in runs (vs ~15). Plain: python 17/19 (9.8 min), research 26/27 (2.2 min, report 13 words over), ts 20/22 (44.5 min). Phased: python 18/19 (15.4 min); research 0/27 because explore wrote answers.md/report.md into the work folder and the plan ended with no phases (27/27 when moved); ts phased stopped by the user at ~115 min (not graded); writing-story not run. Write-up in its `ASSESSMENT.md`, archived in `~/.local/share/tai/bench-archive/`. Phase prompt causes found in `phases.py`: explore forbids project changes but the work folder is writable, so deliverables land there; the plan prompt invites tool/test-building phases (writing-story's checker). Each phase re-reads all sources.
- **Done 2026-11-16:** phase prompt fixes (deliverables in the project, short plan, no tool/test phases unless needed, empty plan refused); quick suite (`--quick`: q-ts-scopes, q-python-clock, q-research-ferry, q-story-letter, graders checked); variants (`-V`, `benchmarks/variants/`: plain, phased, checklist, lean, checklist-lean, phased-think-plan, plan-only); process metrics + aihealth health per run (third report table). Phase replay from a transcript was dropped: the repo state at a phase start isn't recorded; `plan-only` (stop after plan) covers the early phases, where both failures happened.
- **Quick run (2026-11-15, Turbo, `~/.local/share/tai/bench/20261116-quick-1`, write-up in its `ASSESSMENT.md`):** plain / lean / phased 100% on all four quick tasks, checklist-lean 70%. Total minutes: lean 15.1, plain 22.1, checklist-lean 23.1, phased 26.9. The suite is too easy to separate quality; lean is fastest at equal score; the checklist review turn churned (30 word-count edits, ended over the limit). Fixed on the way: python graders missed tests inside classes; device-broken replies were graded as agent failures (now `chat_stream` raises on an error chunk or no `[DONE]`, runs with `device_errors` are kept as `-error`, retried once, left out of the report); phase prompts now give full work-folder paths (notes/plan landed in the project root).
- **Code grade (2026-11-16):** tests can't see it, aihealth can: q-ts-scopes all pass, but `template.ts`-level code health is plain 8.95, phased 8.09, checklist-lean 6.95, lean 6.54 (ref 7.90). ts-templates plain 5.3–5.8 vs ref 8.65 in both full runs. Python tasks: all variants ≈ ref (9.2–9.5). So lean's speed on code may come from cramming logic into one function: check with `-n 3` before making lean the default.
- **Shape run (2026-11-16, Turbo, `~/.local/share/tai/bench/20261116-shape-1`, write-up in its `ASSESSMENT.md`):** q-python-clock + q-ts-scopes × plain/lean/lean-shape/lean-idioms × 3. No variant clearly wins: spread within a variant (ts code 1.2–2.0 points, ts minutes 3.6–21.4) is larger than between them. ts code: plain 8.00, lean-shape 7.52, lean-idioms 7.44, lean 7.16 (ref 7.90); ts minutes: lean-idioms 8.0, lean-shape 8.8, plain 10.2, lean 13.6; plain had the one real ts failure (stopped at 67% after 2.5 min). Python: 7 of 12 runs, in every variant, ignored the task's explicit "call `clock.now()` through the module" (imported `now` directly), so the grader is right. Long runs are edit → test fix loops (14–42 test runs vs 4). Idioms showed no effect.
- **lean-check run (2026-11-16, `~/.local/share/tai/bench/20261116-check-1`, write-up in its `ASSESSMENT.md`):** plain / lean-shape / lean-check (lean-shape + re-check requirements before finishing, stop fix loops after 3 failures) × 3. lean-check didn't help: it re-read its files but still missed an explicit requirement (IDs reused after a release) and once stopped at 67% on ts; +60% time on python. Over six runs (with the shape run) lean-shape is best: python 97% / ts 99% vs plain 95% / 94%, ts 6.8 vs 9.6 min, code 7.14 vs 7.38 (noise; ref 7.90). Misses come from habits (`len(dict) + 1` IDs, `from clock import now`) that contradict a sentence in the task.
- **plain-structured (2026-11-16, `~/.local/share/tai/bench/20261116-structured-1`):** plain + "Write well-structured code." × 3: no gain (ts code 6.80 vs plain's 7.38, python 9.16 vs 9.36), slower (ts 17.5 min, one 31-min fix loop). A generic quality line does nothing for Turbo.
- **scripted run (2026-11-17, `~/.local/share/tai/bench/20261116-scripted-1`, write-up in its `ASSESSMENT.md`):** plain vs scripted ("each tool call costs a model turn; batch looking around/checking into one script") × 3 on research, python, ts. Turbo did not batch (still one read per file), but ran tests and edited less: ts 8.1 → 4.7 min with no fix loop, python 3.9 → 2.8, scores equal or better, code grade unchanged. Code Mode proper (a `run_code` tool calling tai tools) parked: the model doesn't batch even when told.
- **scripted, 5 runs (2026-11-17, `~/.local/share/tai/bench/20261117-scripted-5`, write-up in its `ASSESSMENT.md`):** the n=3 gain didn't hold. ts scripted median 4.7 vs plain 7.2 min, but mean 14.7 vs 7.1: one 58-min run where it wrote a dozen throwaway debug scripts, and one run that ended after a single "Let me start by exploring…" reply with no tool call (the loop takes that as done). Python equal. scripted is not going into `STYLE`.
- **Quality run (2026-11-17, `~/.local/share/tai/bench/20261117-quality-1`, write-up in its `ASSESSMENT.md`):** plain vs bare × 3 on q-py-bulk and q-py-pagefix, judged by `review.py`. All runs pass every test with equal aihealth; Claude review 2 plain, 3 bare, 1 tie, mostly ties per criterion: `STYLE` makes no visible quality difference on small steps. The review found concrete issues tests missed (summed instead of per-line rounding, a no-op guard, an unused import, a half regression test) and a hole in a hidden test (fixed; regrading confirms it).
- **Next (resume here):** `STYLE` is in tai agent's prompt (2026-11-16). `default.agent` = Turbo (user setting, 2026-11-16). Ideas not tried: the loop re-showing the task's explicit requirements when the model says it's done, measured with `review.py`; plain vs no-nudges; a bigger code task or n ≥ 5 to compare code quality; something against fix loops (15–30 min runs) and habitual misses of explicit requirements.

## Open questions
- None right now.
