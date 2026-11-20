# TiinyOS desktop app as an API reference

The TiinyOS app (`/Applications/TiinyOS.app`, Electron, read at version 1.0.3 on 2026-11-20) is the most complete client of the device. When the device's `/openapi.json` and the official CLI don't show how something is done, the app's code does. `tai update`, `tai rm` and downloads in `tai status` came from it.

## Where the code is

- `Contents/Resources/app.asar` (160 MB, one archive). The app's own code is minified JavaScript under `/out/renderer/js/*.js` (the UI; almost all device calls) and `/out/main/index.js` (10 MB; upgrades, connectors, vendor SDKs).
- Search the archive as bytes; no unpacking needed. `grep -o` with a long `.{n}` context is too slow on it (minutes). Use Python:

```python
import mmap, re
f = open("/Applications/TiinyOS.app/Contents/Resources/app.asar", "rb")
m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
for hit in re.finditer(rb"/api/v1/models/\$\{[^}]*\}/update", m):   # a route, a field name, a UI string
    print(m[hit.start() - 400: hit.end() + 900].decode("utf8", "replace"), "\n----")
```

- Read around a hit for the HTTP method, the body, and which response fields the app reads (it lists them one by one, which gives the response shape).
- Many hits are not the device: `/out/main/index.js` bundles vendor SDKs (`/v1/agents`, `/v1/libraries`, `/v1/fine_tuning`, Google `:signBlob`), and firmware-upgrade fields are also called `need_update`. Confirm a route against the device before using it.
- The app sends model ids with `encodeURIComponent` (`Qwen%2FQwen3.8-27B`); the plain slash that tai sends works too.
- Probe a write route safely with an id that doesn't exist (`DELETE /api/v1/models/nobody/none` → 200 "already deleted").

## Routes found there (GETs checked live 2026-11-20 unless marked)

Models:
- `GET|POST /api/v1/models/{id}/update`, `DELETE /api/v1/models/{id}`, `?including_downloading=true`: in use by tai.
- `GET /api/v1/models/types` → `data: [{type, label, count}]` (12 types).
- `GET /api/v1/models/storage` → per model `size_bytes`, `docker_name` (image:version), shared/unique layer sizes; totals.
- `GET /api/v1/models/{id}/unload_candidate` → `unload_required`, `candidate`, `npu_required/available/used`: the device's own pick of what to unload for a model.
- `GET /api/v1/model-stats` → `items: [{model_name, downloads, likes, liked}]` for the store.
- `POST /api/v1/models/{id}/pause_download`; `…/download/ws`, `/api/v1/models/running/ws`, `/api/v1/models/npu/status/ws` (WebSocket twins of the streams); `POST /api/v1/models/unload_all`; `POST /api/v1/models/{id}/launch` (no stream). Not tried.
- `/api/v1/models/import`, `import/inspect`, `import/toolkits`: import from Hugging Face. Not tried.

System:
- `GET /api/v1/npu/status` → `devices: [{util_percent, mem_used_mb, mem_total_mb, temp_c, power_w, model, sn}]`, `cpu`, `memory`, `npu {status, status_message}`: NPU temperature and power, which `/api/v1/models/npu/status` doesn't have.
- `GET /api/v1/sys/storage` → `total_bytes, used_bytes, remaining_bytes, details: [{name, size_bytes}]`; `GET /api/v1/sys/storage/agent` → `apps: [{name, size_bytes}]`.
- `GET /api/v1/sys/device_info` → model name, `tiiny_os`, cpu, gpu, dnpu, ram, storage, version.
- `GET|POST /api/v1/sys/ac_power/` → `{status, ac_power}`. `POST /api/v1/sys/shutdown/`, `/api/v1/sys/wifi/*`: not tried.
- `GET /api/v1/statistics/week_usage`, `app_week_usage`: 422 without query parameters (not worked out).

Services (apps running on the device, separate from models):
- `GET /api/services` → `[{id, name, description, ports, cloud_version, status}]` (2 on ours); `GET /api/services/updates/check` → `[{service_id, current_version, latest_version, update_available}]`.
- `POST /api/services/{id}/start/stream`, `/stop`, `/update/stream`, `DELETE /api/services/{id}`: not tried.

Knowledge base (`/kb`):
- `GET /kb/memory/model` → `{model_id, automatic_summary_enabled}`; `GET|PUT /kb/memory/schedule` → the nightly summary window (23:00–05:00 on ours).
- `GET /kb/sources/files`, `GET /kb/sources/conversations` → paged `items` (`source_id, filename, chunk_count, finalized, vectorize_status`); `GET /kb/user-context` → the profile the device adds to chats.
- `POST /kb/delete-memory`, `PATCH /kb/sources/{id}`, `/kb/upload/stream`, `/kb/finalize/stream`: not tried.

Not on the device gateway (404 there; they belong to the app's local connector server or the cloud): `/v1/backup`, `/v1/restore/sse`, `/v1/config/*`, `/v1/gmail|outlook|calendar|telegram|discord|whatsapp|x/*`, `/v1/credits`, `/v1/status`, `/v1/data/state`, `/api/upgrade/*`, `/api/v2/*`.

Recheck after an app update: routes and field names can change, and the bundle file names carry a hash.
