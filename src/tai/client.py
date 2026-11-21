"""Thin HTTP client for the Tiiny device.

Everything goes through the nginx gateway on port 80 with a Bearer token.
Two API families live there:

* ``/api/v1/...``  — device management (model catalog, load/unload, NPU).
* ``/v1/...``      — inference, OpenAI-compatible where possible. Only models
  that are currently *loaded* answer; others return 404 model_not_found.

See cards/device-api.md for the full endpoint map and quirks.
"""

from __future__ import annotations

# The device's HTTP API takes model ids, paths and form fields as plain strings.
# aihealth: disable=primitive_obsession

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from tai import hfimport
from tai.config import Settings


LAUNCH_IDLE_TIMEOUT = 180.0  # seconds without a progress event before a load counts as failed
DOWNLOAD_IDLE_TIMEOUT = 300.0  # the same for a download (it reports about once a second)


class TiinyError(RuntimeError):
    pass


def _launch_event(event: str, data: str) -> dict:
    try:
        payload = json.loads(data)
    except ValueError:
        payload = {"message": data}
    payload["_event"] = event
    return payload


def _message(e: Any) -> str:
    """The message of an error object from the device (a dict with "message", or anything else)."""
    return str(e.get("message") or e) if isinstance(e, dict) else str(e)


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text


def _err(r: httpx.Response) -> TiinyError:
    body = _body(r)
    msg = body
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict):
            msg = e.get("message") or e
        elif e:
            msg = e
        else:
            msg = body.get("message") or body.get("detail") or body
    return TiinyError(f"HTTP {r.status_code} {r.request.method} {r.request.url.path}: {msg}")


# Seconds without any data before a chat stream counts as dead. The device has dropped a stream while
# keeping the connection open (a benchmark run waited 25 min). With return_progress it sends events
# while reading the prompt too, so silence means trouble sooner; without, the first token can take
# as long as the device's own ~10-minute request limit.
STREAM_IDLE_PROGRESS = 180.0
STREAM_IDLE = 600.0


def iter_sse(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    """Yield (event, data) pairs from a text/event-stream line iterator."""
    event, data = "message", []
    for line in lines:
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield event, "\n".join(data)


CONNECT_RETRIES = 2  # extra connection attempts (at once, then 0.5 s later); a sent request is never repeated
CONNECT_TIMEOUT = 5.0  # per attempt, so a device that is off is reported after ~15 s


class TiinyClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.settings = settings
        self.http = http or httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=httpx.Timeout(30.0, read=None, connect=CONNECT_TIMEOUT),
            # Wi-Fi to the device drops single connection attempts ("No route to host"); the next one works
            transport=httpx.HTTPTransport(retries=CONNECT_RETRIES),
        )

    # -- helpers ---------------------------------------------------------
    def _json(self, method: str, path: str, **kw) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise _err(r)
        return r.json()

    def _sse(self, method: str, path: str, **kw) -> Iterator[tuple[str, str]]:
        with self.http.stream(method, path, **kw) as r:
            if r.status_code >= 400:
                r.read()
                raise _err(r)
            yield from iter_sse(r.iter_lines())

    # -- device management ----------------------------------------------
    def models(self) -> list[dict]:
        """Every model installed on the device, loaded or not (has npu_usage, status)."""
        return self._json("GET", "/api/v1/models/")["data"]

    def running(self) -> dict:
        return self._json("GET", "/api/v1/models/running")

    def npu(self) -> dict:
        return self._json("GET", "/api/v1/models/npu/status")

    def launch(self, model_id: str) -> Iterator[dict]:
        """Load a model; yields progress dicts, the last one has event 'done'.

        The device sends progress every second or two. It can also silently drop a
        launch (seen while a 3D job was busy) and leave the stream open, so a
        stream with no event for LAUNCH_IDLE_TIMEOUT seconds is treated as failed."""
        path = f"/api/v1/models/{model_id}/launch/stream"
        timeout = httpx.Timeout(30.0, connect=CONNECT_TIMEOUT, read=LAUNCH_IDLE_TIMEOUT)
        try:
            for event, data in self._sse("POST", path, timeout=timeout):
                yield _launch_event(event, data)
        except httpx.ReadTimeout as e:
            raise TiinyError(
                f"No progress from the device for {LAUNCH_IDLE_TIMEOUT:.0f}s while loading {model_id}; "
                "it may have dropped the request. Check `tai status` and retry."
            ) from e

    def store_models(self) -> list[dict]:
        """Every model in the Tiiny store (name, fullname = id, type, desc), downloaded or not."""
        return self._json("GET", "/api/v1/models/online_models")

    def download(self, model_id: str) -> Iterator[dict]:
        """Download a store model to the device; yields events like ``launch``: ``init``, then
        ``progress`` (status, progress %, stage, downloaded/total_bytes, speed_human, error) and
        ``done``. Not in the device's OpenAPI; found in the official tiiny CLI (2026-11-16)."""
        path = f"/api/v1/models/{model_id}/download/stream"
        timeout = httpx.Timeout(30.0, connect=CONNECT_TIMEOUT, read=DOWNLOAD_IDLE_TIMEOUT)
        try:
            for event, data in self._sse("POST", path, timeout=timeout):
                ev = _launch_event(event, data)
                if ev.get("error") or event == "error":
                    raise TiinyError(f"download of {model_id} failed: {_message(ev.get('error') or ev)}")
                yield ev
        except httpx.ReadTimeout as e:
            raise TiinyError(f"No progress from the device for {DOWNLOAD_IDLE_TIMEOUT:.0f}s while downloading "
                             f"{model_id}. Check `tai models`; the device may still be downloading.") from e

    def import_toolkits(self) -> list[dict]:
        """The converters the device can import Hugging Face models with (``display_name``/``id``,
        ``compatibility``). From the TiinyOS app (2026-11-21), like the two below."""
        return self._json("GET", "/api/v1/models/import/toolkits").get("toolkits") or []

    def import_inspect(self, hf_url: str) -> dict:
        """What the device says about importing a Hugging Face model (``hfimport.Inspection``)."""
        r = self.http.post("/api/v1/models/import/inspect", json={"hf_url": hf_url}, timeout=60.0)
        if r.status_code >= 400:
            raise TiinyError(f"can't import {hf_url}: {hfimport.error_text(_body(r))}")
        return r.json()

    def import_model(self, request: dict) -> Iterator[dict]:
        """Start an import (``Inspection.request``) and follow it: yields progress events until done."""
        timeout = httpx.Timeout(30.0, connect=CONNECT_TIMEOUT, read=DOWNLOAD_IDLE_TIMEOUT)
        try:
            for event, data in self._sse("POST", "/api/v1/models/import", json=request, timeout=timeout):
                ev = _launch_event(event, data)
                state = hfimport.event_state(event, ev)
                if state == "error":
                    raise TiinyError(f"import failed: {hfimport.error_text(ev.get('error') or ev)}")
                yield ev
                if state == "done":
                    return
        except httpx.ReadTimeout as e:
            raise TiinyError(f"No progress from the device for {DOWNLOAD_IDLE_TIMEOUT:.0f}s while importing. "
                             "Check `tai status`; the device may still be working on it.") from e

    def downloading(self) -> list[dict]:
        """Catalog rows of models the device is downloading right now (``progress`` in %)."""
        rows = self._json("GET", "/api/v1/models/", params={"including_downloading": "true"})["data"]
        return [d for d in rows if d.get("status") == "downloading"]

    def update_info(self, model_id: str) -> dict:
        """``need_update``, ``local_version``, ``cloud_version``, ``update_desc``, ``disk_ok``,
        ``required_bytes``/``available_bytes`` for an installed model (from the TiinyOS app, 2026-11-20)."""
        return self._json("GET", f"/api/v1/models/{model_id}/update")

    def start_update(self, model_id: str) -> dict:
        """Delete the installed copy and start downloading the store's version; follow it with
        ``download``. A 200 with nothing to update just says so."""
        return self._json("POST", f"/api/v1/models/{model_id}/update", timeout=60.0)

    def delete(self, model_id: str) -> dict:
        """Delete a downloaded model from the device's disk. The device answers 200 "already
        deleted" for an id it doesn't have."""
        return self._json("DELETE", f"/api/v1/models/{model_id}", json={"model_id": model_id}, timeout=300.0)

    def stop(self, model_id: str) -> dict:
        return self._json("POST", f"/api/v1/models/{model_id}/stop")

    def server_version(self) -> dict:
        """Client mode: the `tai serve` it goes through ({version, build})."""
        return self._json("GET", "/_tai/version")

    # -- device ----------------------------------------------------------
    def sys_status(self) -> dict:
        """CPU, memory, NPU and disk utilisation."""
        return self._json("GET", "/api/v1/sys/status")

    def npu_chip(self) -> dict:
        """The NPU chip's own readings: ``temp_c``, ``power_w`` (both null on our device, 2026-11-20),
        ``util_percent``, ``mem_used_mb``/``mem_total_mb``, ``model``. {} if the device lists no chip."""
        return (self.npu_hardware().get("devices") or [{}])[0]

    def npu_hardware(self) -> dict:
        """What the TiinyOS Dashboard's gauges read: ``devices[0].util_percent`` (NPU), ``cpu.total_percent``
        (SoC), ``memory.usage_percent`` (RAM), plus ``npu {status, status_message}`` and ``occupants``."""
        return self._json("GET", "/api/v1/npu/status")

    def week_usage(self, start_date: str) -> dict:
        """Tokens and requests per day and model for the seven days from ``start_date`` (YYYY-MM-DD):
        ``{day: {model_id: {prompt_tokens, completion_tokens, total_tokens, request_count}}}``."""
        return self._json("GET", "/api/v1/statistics/week_usage", params={"start_date": start_date})

    def device_info(self) -> dict:
        """Model name, OS version, hardware specs (slow: ~1-2 s)."""
        return self._json("GET", "/api/v1/sys/device_info")

    def storage(self) -> dict:
        return self._json("GET", "/api/v1/sys/storage")

    def reboot(self) -> dict:
        return self._json("POST", "/api/v1/sys/reboot/")

    def shutdown(self) -> dict:
        return self._json("POST", "/api/v1/sys/shutdown/")

    # -- inference -------------------------------------------------------
    def chat(self, model: str, messages: list[dict], **extra) -> dict:
        body = {"model": model, "messages": messages, "stream": False, **extra}
        return self._json("POST", "/v1/chat/completions", json=body)

    def chat_stream(self, model: str, messages: list[dict], **extra) -> Iterator[dict]:
        """Chunks of a streamed chat reply. Raises TiinyError when the device sends an error chunk
        the stream stops before ``[DONE]`` (seen: a reply cut off mid tool call, then empty
        replies for a few seconds), or no data arrives for a while, so a broken reply isn't taken for a finished one."""
        body = {"model": model, "messages": messages, "stream": True, **extra}
        idle = STREAM_IDLE_PROGRESS if extra.get("return_progress") else STREAM_IDLE
        try:
            for _event, data in self._sse("POST", "/v1/chat/completions", json=body,
                                          timeout=httpx.Timeout(30.0, connect=CONNECT_TIMEOUT, read=idle)):
                if data.strip() == "[DONE]":
                    return
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise TiinyError(f"the device sent an error mid-reply: {_message(chunk['error'])}")
                yield chunk
        except httpx.TransportError as e:
            raise TiinyError(f"the connection to the device broke mid-reply: {e}") from e
        raise TiinyError("the device ended the reply early (no [DONE]): it may be overloaded or restarting")

    def embed(self, model: str, inputs: list[str], **extra) -> dict:
        return self._json("POST", "/v1/embeddings", json={"model": model, "input": inputs, **extra})

    def image(self, model: str, prompt: str, size: str = "1024x1024", **extra) -> bytes:
        body = {"model": model, "prompt": prompt, "size": size, "n": 1, **extra}
        d = self._json("POST", "/v1/images/generations", json=body)
        return base64.b64decode(d["data"][0]["b64_json"])

    def tts(self, model: str, text: str, config: dict | None = None) -> tuple[bytes, dict]:
        """Returns (wav bytes, metadata). Uses the native /v1/synthesize route:
        the OpenAI-style /v1/audio/speech fails upstream for OmniVoice."""
        body: dict = {"model": model, "text": text}
        if config:
            body["config"] = config
        d = self._json("POST", "/v1/synthesize", json=body)
        if not d.get("success", True):
            raise TiinyError(f"TTS failed: {d.get('error')}")
        audio = base64.b64decode(d.pop("audio_data"))
        return audio, d

    def asr(self, model: str, path: Path, fields: dict[str, str] | None = None) -> dict:
        """``fields``: further form fields, e.g. ``{"language": "sv"}``."""
        data = {**(fields or {}), "model": model}
        with open(path, "rb") as f:
            return self._json(
                "POST", "/v1/audio/transcriptions", data=data, files={"file": (path.name, f)}
            )

    # -- image-to-3D (TRELLIS) — gateway maps /v1/3d/* to the model's /v1/* ----
    def task3d_submit(
        self,
        stage: str,
        *,
        image: Path | None = None,
        params: dict | None = None,
        input_refs: dict | None = None,
    ) -> str:
        data = {"stage": stage}
        if params:
            data["params"] = json.dumps(params)
        if input_refs:
            data["input_refs"] = json.dumps(input_refs)
        files = None
        if image:
            files = {"image": (image.name, image.read_bytes())}
        d = self._json("POST", "/v1/3d/tasks", data=data, files=files)
        return d["data"]["task_id"]

    def task3d_capabilities(self, model: str | None = None) -> dict:
        """The 3D model's stages, inputs and per-stage `stage_specs` (params + controls it accepts).
        ``model`` picks one when several 3D models are loaded."""
        params = {"model": model} if model else None
        return self._json("GET", "/v1/3d/capabilities", params=params).get("data") or {}

    def capabilities(self, model: str) -> dict:
        """What a loaded chat or TTS model reports about itself (inputs, thinking, voice options)."""
        return self._json("GET", "/v1/capabilities", params={"model": model})

    def task3d(self, task_id: str) -> dict:
        return self._json("GET", f"/v1/3d/tasks/{task_id}")["data"]

    def task3d_artifact(self, task_id: str, artifact_id: str) -> bytes:
        return self._bytes(f"/v1/3d/tasks/{task_id}/artifacts/{artifact_id}")

    # -- video (MiniMax-H3-Turbo): OpenAI-style jobs ------------------------
    def video_info(self, model: str) -> dict:
        """A loaded video model's options: `canvases` (the only sizes), `step_options`, `fps`, ..."""
        d = self._json("GET", "/v1/video/info", params={"model": model})
        return d["data"] if isinstance(d.get("data"), dict) else d

    def video_submit(self, model: str, prompt: str, **fields) -> str:
        """Start a render; ``fields``: size "WxH", seconds (a string), steps, seed, image (data URL)."""
        return self._json("POST", "/v1/videos", json={"model": model, "prompt": prompt, **fields})["id"]

    def video(self, video_id: str) -> dict:
        """`status` queued → in_progress → completed / failed, `progress` 0-100, `error`."""
        return self._json("GET", f"/v1/videos/{video_id}")

    def video_content(self, video_id: str) -> bytes:
        return self._bytes(f"/v1/videos/{video_id}/content")

    def _bytes(self, path: str) -> bytes:
        r = self.http.get(path)
        if r.status_code >= 400:
            raise _err(r)
        return r.content
