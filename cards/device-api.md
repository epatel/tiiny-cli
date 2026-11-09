# device-api

The Tiiny device's HTTP API as `tai` uses it: endpoints, payloads and their quirks. The device has no published API docs, so everything here was worked out by probing the device directly.

## Access

- Everything goes through an nginx gateway on **port 80** of the device (`deviceAddress` in `~/.local/tiiny/config.json`).
- Auth: `Authorization: Bearer <token>` (or `x-api-key`). The token is `account.authKey` from that same config, written by `tiiny login`. `src/tai/config.py` reads it. Overrides, highest first: real env, then `./.env` (`TAI_DEVICE`; token from `TAI_TOKEN`, then `TIINY_API_KEY`). Empty values count as unset.
- `GET /` needs no auth and returns the server version. `GET /openapi.json` covers only the system/account routes, not models or inference.
- Errors come in several shapes: `{"error": {"message": …}}` (OpenAI style), `{"error": "…", "error_code": …}` (model containers), and `{"detail": …}` (FastAPI). `client._err` flattens all of them.

## Management (`/api/v1/models`)

| Call | Purpose |
|---|---|
| `GET /api/v1/models/` | **Every installed model**, loaded or not: `id`, `display_name`, `type`, `npu_usage`, `status` (`running`/`downloaded`), `input` ("Text,Image,Video"), `thinking` |
| `GET /api/v1/models/running` | Loaded instances with `use_time`, `last_inference_time`, `active_request_count`, internal `port` |
| `GET /api/v1/models/npu/status` | `npu_total` (100), `npu_used`, `npu_available` |
| `POST /api/v1/models/{id}/launch/stream` | Load. SSE stream of `event: progress` then `event: done`; on failure `done` has `success: false`, plus `startup_error` and a `toast.message` |
| `POST /api/v1/models/{id}/stop` | Unload |

Model ids contain a slash (`Qwen/Qwen3-Coder-Next`), and the slash goes straight into the path.

## Inference (`/v1`): only *loaded* models answer

`GET /v1/models` lists only loaded models. Calling one that isn't loaded returns 404 `model_not_found`.

| Task | Call | Notes |
|---|---|---|
| chat / vision | `POST /v1/chat/completions` | OpenAI format, SSE streaming. Images go in as `image_url` data URLs. Reasoning streams in `delta.reasoning_content`. Thinking is toggled with `chat_template_kwargs: {enable_thinking: bool}`; `reasoning_effort` is ignored |
| embeddings | `POST /v1/embeddings` | OpenAI format; Qwen3-Embedding returns 1024 dims |
| image | `POST /v1/images/generations` | OpenAI format, returns `b64_json`, `size: "WxH"`. Also takes `seed`, `steps`, `guidance_scale`, `negative_prompt`. The seed used is not returned, so `tai image` always sends one (random by default) and prints it. `POST /v1/image/generate` returns raw PNG bytes instead |
| tts | `POST /v1/synthesize` `{model, text, config?}` | Returns JSON `{audio_data: base64 WAV 24 kHz mono, duration, …}`. Voice design goes in as `config: {instruct: "female, british accent"}`; a top-level `instruct` is rejected. **`/v1/audio/speech` returns 500 "upstream TTS model failed" for OmniVoice, so don't use it.** Valid instruct words: `male/female`, age groups, `low/high pitch`, `whisper`, accents |
| asr | `POST /v1/audio/transcriptions` | multipart `model`, `file`, optional `language`; returns `{text}` |
| 3d | `/v1/3d/*` | The gateway maps this onto the TRELLIS container's `/v1/*`. Multi-stage pipeline: shape → texture → (simplify) → export, polled per task. Any 5xx during polling (e.g. 504) is transient |

### Asking a model for its options (`tai info`)

Only some models describe themselves, and only while loaded (checked 2026-11-09). `src/tai/info.py` maps task → endpoint:

- chat and TTS: `GET /v1/capabilities?model=<id>`. Chat gives modalities, `supported`, `thinking` (no sampling params; those are plain OpenAI fields). OmniVoice gives `capabilities`, `duration` limits, `instruct_list` and `instruct_exclusive_groups` (pick at most one per group).
- 3D: `GET /v1/3d/capabilities` (`?model=` when two 3D models are loaded): stages, qualities, inputs and params per stage, `stage_specs`.
- Image, ASR, embedding: none. `/v1/capabilities` answers 400 "Model … does not support tts", so `tai info` doesn't call it for them. Not loaded → 404 "is not loaded".
- Video (`lightx2v/MiniMax-H3-Turbo`, type Text-to-Video, installed but unsupported by tai): the community notes say `/v1/video/info`; not checked.

`tai info` asks before loading a model to query it (`-y` loads, `--no-load` never does).

### 3D pipeline details

Implemented in `src/tai/mesh.py`: `Mesh3D.generate` runs the stages, `run` submits and polls one stage (retrying gateway 5xx), and `StageResult.ref` builds input references.

- `POST /v1/3d/tasks` (form fields): `stage`, `image` (file), `params` (JSON string), `input_refs` (JSON string of `{input_name: {task_id, artifact_id}}`).
- `GET /v1/3d/tasks/{id}` → `data.status` (`queued`/`running`/`completed`/`failed`), `progress`, `message`; when completed, `result.output.artifacts[]`, each with `artifact_id`, `artifact_type` and `filename`.
- `GET /v1/3d/tasks/{id}/artifacts/{artifact_id}` downloads an artifact. `GET /v1/3d/capabilities` lists the stages and their params. TRELLIS rejects params a stage doesn't list in `stage_specs[stage]` (`params` keys + `controls`), so `tai 3d` filters `inference_quality`/`seed` per stage (`MeshOptions.controls_for`).
- Inputs per stage: shape takes `image`; texture and simplify take `shape_artifact`; export takes `shape_artifact` + `material_artifact` (texture's output), optionally `geometry_artifact` (simplify's output), and returns `mesh_glb`.
- Rough timings: shape ≈ 4 min, texture ≈ 2 min, export ≈ 8 min. An unsimplified GLB is about 120 MB. With simplify (`target_faces: 200000`, ≈ 25 s) export drops to ≈ 25 s and the GLB to about 16 MB. Simplify outputs an artifact of type `geometry…`.

## Rediscovering the API

Each loaded model runs in a container whose port shows up in `/api/v1/models/running`. Those ports are reachable on the LAN without auth, and `GET http://<ip>:<port>/` or `/openapi.json` usually lists that container's real routes. Use them to discover endpoints, but call through the gateway in code. Strings in the official `tiiny` binary (`~/.local/tiiny/bin/tiiny-cli-*`) also reveal route templates.
