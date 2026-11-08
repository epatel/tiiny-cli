# tiiny-api-notes

A digest of the community notes at https://github.com/thezolon/tiiny-api-notes (unofficial, Apache-2.0, by a Tiiny owner, checked up to TiinyOS 0.1.39 on 2026-11-06). It covers device behaviour and endpoints beyond what `tai` uses, compared with what we saw on our own device (TiinyOS 0.1.39, services 0.1.30). The notes mark each claim **tested / spec / docs / app**; treat anything not marked tested as a lead, and re-check on our device before relying on it.

## Device behaviour that affects tai

| finding (notes) | what it means for tai | status in tai |
|---|---|---|
| **After a power cycle every call answers `502 Bad Gateway` until the device is unlocked** by `tiiny login` (enough on a signed-in PC) or by opening the TiinyOS app. After that it stays unlocked, even with the app closed. TiinyOS "Auto Start" avoids it. A call *without* a key then gets 401, not 502. | `/tiiny restart` brings the device back **locked**, so every `tai` call will 502. A 502 from any endpoint most likely means "locked": suggest `tiiny login`. | not handled; errors surface as plain HTTP 502 |
| **One request at a time.** Parallel requests to different models slow each other down badly. | Don't run inference concurrently (e.g. image generation while the agent streams). | `tai` is sequential; the agent's tools run between model calls |
| **Chat requests are cut off after ~600 s** with `504 "Upstream model server timed out."` (128K prompts on dense models hit it; MoE models are safer). Jev/`systemone` requests: 240 s. Video renders: 1 hour (and the render keeps running on the NPU). | Long agent sessions near 100K+ tokens can hit the 504. Streaming may not avoid it (untested). | not handled; no context compaction yet |
| **First request right after a load can fail with `xh2a backend fail`** (seen with Qwen-Image): wait ~10 s and retry. | Worth a single retry after `ensure_loaded` for image/chat. | not handled |
| **Thinking counts toward `max_tokens`**: a small limit can return empty `content` with `finish_reason: "length"`. Turn thinking off (`chat_template_kwargs.enable_thinking: false`; gpt-oss uses `reasoning_effort`; Anthropic style `"thinking": {"type": "disabled"}`). | Matches tai's `--no-think`/`think` setting; avoid small `max_tokens` with thinking models. | handled by not setting `max_tokens` |
| **Streaming:** the last chunk before `[DONE]` can carry `usage` with an empty `choices` list. | Never index `choices[0]` blindly. | handled (`ask`/`chat` skip it; the agent uses `(choices or [{}])[0]`) |
| **`unload_candidate` can say a model fits while the load never starts** when the NPU is nearly full; unload others first. | Same family of problem as the "Cannot allocate memory" fragmentation tai already works around. | tai retries after unloading all idle models |

## Endpoints tai doesn't use yet (all on port 80, Bearer key)

- **Models:** `GET /api/v1/models/{id}/unload_candidate` returns the device's own swap plan (`unload_required`, `model_id[]`, `npu_required/available`). `POST /api/v1/models/unload_all`. `POST /api/v1/models/{id}/launch` (async, poll `running`) as an alternative to `launch/stream`. WebSockets `/api/v1/models/running/ws` and `/api/v1/models/npu/status/ws` for live status (**app**). `?including_downloading=true` shows download progress. The notes URL-encode ids (`Qwen%2FQwen3-…`); tai sends the raw slash, which also works.
- **Images:** `/v1/images/generations` also takes `negative_prompt`, `seed`, `steps`, `guidance_scale`, and **editing** via `image` (one data URI) or `images` (up to 10) with an edit-capable model. `n > 1` fails: one image per request.
- **OCR:** `POST /v1/ocr?model=<id>` `{"input": data URI, "response_format": "json"}` → `data[0].text` (PP-OCRv6 works; GLM-OCR only on its own container port with the prompt `Text Recognition:`).
- **Rerank:** `POST /v1/rerank {model, query, documents[]}` (**docs**).
- **Music:** `/v1/music/*` (Foundation-1 loops via `/generate/wav`; SongGeneration via `/generate/preview` + `/resume`).
- **Video** (MiniMax-H3-Turbo, 0.1.38+): `POST /v1/videos` → poll `GET /v1/videos/{id}` → `GET …/content` (MP4). Fixed canvases only, max 7 s, minutes to an hour to render.
- **Text classification ("Jev / System One", 0.1.39+):** `POST /v1/systemone {model, state, questions}` with `noul` (yes/no), `choice` and `score` question types, up to 64 per request. Answers come back as probabilities, with no text generated.
- **Other API styles on the same port:** Anthropic `POST /v1/messages` (accepts `x-api-key`) and Ollama `/api/chat`, `/api/tags`. Host names `openai.api.tiiny`, `anthropic.api.tiiny`, `ollama.api.tiiny`, `api.tiiny` all map to port 80. Model `default` = the loaded chat model.
- **System:** `GET/POST /api/v1/sys/ac_power/` (power on when plugged in). `POST /api/v1/sys/shutdown/` works since 0.1.37 (keep the trailing slash; off in ~10 s; on 0.1.36 it answered ok but stayed on). `/api/v1/sys/reboot/` is **spec** only. Wi-Fi setup routes 404 over Wi-Fi.
- **Discovery (no auth):** `GET :39218/device.json` (name, IPv4/IPv6, USB link addresses), UDP 39217 broadcast `GADGET_DISCOVER_V1`, mDNS `_gadget._tcp`.
- **Vault / knowledge base `/kb/*`** ("SimpleMem", has its own spec at `/kb/openapi.json`): upload → `finalize` (needs an **embedding model loaded**) → `retrieve` (wants a chat model too unless `fast_mode: true`; `include_memory_context` defaults to **true** and mixes in personal memory) → rename / download / `delete-memory`. Memory pipeline: conversation days, nightly "dreaming" 23:00–05:00 into facts and proposals.
- **On-device assistant:** connectors (Gmail, Outlook, Telegram, Discord, WhatsApp…) and its own MCP server registry at `/v1/mcp/servers` (**docs**, untested).

## Extra details from Tiiny Studio's code

https://github.com/thezolon/tiiny-studio (same author, a web studio on top of the API; read at commit `844816d`, 2026-11-03). Its `docs/DEVICE-API.md` repeats the notes. The details below come from its code (`media.py`, `static/image.js`, `server.py`) and are missing above:

- **Image seed:** the device doesn't return the seed it used. The studio always sends one (random in `0..2**31` unless the user set one) so it can show, copy and reuse it (`tai image` now does the same and prints it). "Vary" means the same settings with a new seed. `n` images = `n` requests with seeds `seed, seed+1, …`. Accepted fields (probed on 0.1.35): `model, prompt, negative_prompt, n, size, seed, steps, guidance_scale` (+ `response_format: "b64_json"`). `steps`/`guidance_scale` are left out when "auto", so the model's own defaults apply. The studio's time estimate assumes a model named `*turbo*` is fast (~10 s per 512² unit).
- **Image sizes:** a model's catalog `desc` can list its verified sizes (e.g. "512x512 is native … verified: 432x768, 768x432"). Sizes not on that list can come out with seams, so the studio only offers listed sizes when the `desc` has two or more.
- **`xh2a backend fail` retry:** the studio retries up to 6 times, 10 s apart (`STUDIO_WARMUP_WAIT`), not once.
- **TRELLIS controls (answers our open question):** `output_precision`, `inference_quality`, `asset_quality` and `seed` go in each stage's `params` JSON. The studio sends them to every stage, but first filters them by `/v1/3d/capabilities` → `stage_specs[stage].params ∪ .controls`, because TRELLIS rejects params a stage doesn't list (`tai 3d` now does the same filter). On our device (2026-11-08) shape and texture list `seed` and `inference_quality`. Export took 4.5 min with the NPU at 0%, so a quiet NPU there doesn't mean the task is stuck. `simplify` gets only `{"target_faces": N}`; `background` gets `{}`. With two 3D models loaded, add `?model=` to every `/v1/3d` call. Uploading shape + geometry + material together goes over the device's **200 MB request limit**, which is why `input_refs` matters.
- **Hunyuan3D input:** pixels with alpha < 32 are set to 0 before `shape`. Otherwise faint alpha turns into a flat card.
- **ASR:** WAV input is downmixed and resampled to 16-bit 16 kHz mono before `/v1/audio/transcriptions` (48 kHz stereo gives poor results). Other formats are sent unchanged. `language` is sent only when it is set.
- **Per-model options:** `/v1/capabilities?model=` (chat, TTS), `/v1/music/info`, `/v1/3d/capabilities`, `/v1/video/info`. Image, OCR and ASR models have none, so use the catalog row (`supported`, `desc`).
- **Timeouts the studio uses:** image 3600 s, chat 900 s, load/unload 120 s, `unload_all` 180 s, shutdown 20 s.

## Privacy: tai's chats are captured on the device

- **Every chat sent through the gateway's `/v1/chat/completions` is captured** into the vault as that day's "conversation day", not only TiinyOS's own chats. That includes `tai ask`, `tai chat` and `tai agent`. It lands within ~15–50 s (batched to whole minutes); exact duplicates (same prompt *and* answer) are skipped. The text isn't readable or listed through the API, and TiinyOS doesn't show it.
- With a summary model set (`GET/PUT /kb/memory/model`), days are summarised overnight into **facts**. Without one they stay `queued`. Setting `/kb/memory/scope` to `tiinyos` did **not** stop the capture.
- **Delete:** `DELETE /kb/conversation_days/{YYYY-MM-DD}` removes a whole day (no per-chat delete; can't be undone). List days with `GET /kb/conversation_days?limit=500` (oldest first). Facts: `DELETE /kb/memory/facts/{id}`.
- The notes also say the device keeps its own request record that the API can't show or delete.

## Hardware and speed (for planning)

- Two processors, two memory pools: the SoC (ARMv9.2, Mali-G720, 30 TOPS NPU, 32 GB) and the separate "dNPU" (160 BF16 TOPS, 48 GB). The notes infer a CIX P1 SoC. "Mirage" is the engine for non-llama.cpp models; chat models run in **llama.cpp** containers (1 slot, GGUF).
- Writing speed (0.1.38, thinking off): Qwen3.6-35B-A3B ~27 tok/s, gpt-oss-20b ~24, small dense models plateau at ~18, dense 27B ~6–12. Reading ~500–1,900 tok/s, so a 128K prompt takes ~5 min before the first token on the 35B-A3B. MoE models are the best speed/quality choice.
- Rule of thumb from the notes: dense writing speed ≈ 96 GB/s ÷ model size in GB, capped at ~18 tok/s for small models.

## Tiiny CLI quirks (v0.0.5)

- `tiiny run -m <chat model> -f image.png` **doesn't send the image** (the model invents an answer). This is why tai sends images itself.
- `tiiny load` and `tiiny download` **never return when run from a script** (no TTY), even with `-y`, though the work completes.
- `--device-address` takes a bare IP only (port 80 is fixed). Downloads need the device's Wi-Fi with internet; USB alone can't download.

## Notes vs. our device

- **OmniVoice via `/v1/audio/speech`:** the notes (0.1.39) say "no `voice`" works and only long `instruct` sentences fail. **On our device it answered 500 even without `voice`**, so tai uses the container-backed `/v1/synthesize {model, text, config: {instruct}}`. Short instructs ("female, british accent") worked there.
- **Agree:** auth with the CLI's `authKey`; load/unload routes; `/v1/3d/*` stage tasks with `input_refs`; `enable_thinking` toggle; model containers on dynamic ports are reachable on the LAN **without auth** (while loaded, anyone on the network can use them).
- **Licenses:** `k2-fsa/OmniVoice` weights are **non-commercial** (CC-BY-NC); Z-Image-Turbo, Qwen3 models and TRELLIS.2 are Apache-2.0/MIT.

To check a device against the notes' own harness, use `tools/api_snapshot.py --check` (spec diff after updates) and `tools/api_test.py <section>` in that repo. They use only the standard library and need `TIINY_URL` and `TIINY_KEY`.
