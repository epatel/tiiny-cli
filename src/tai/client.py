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

from tai.config import Settings


LAUNCH_IDLE_TIMEOUT = 180.0  # seconds without a progress event before a load counts as failed


class TiinyError(RuntimeError):
    pass


def _launch_event(event: str, data: str) -> dict:
    try:
        payload = json.loads(data)
    except ValueError:
        payload = {"message": data}
    payload["_event"] = event
    return payload


def _err(r: httpx.Response) -> TiinyError:
    try:
        body = r.json()
    except ValueError:
        body = r.text
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


class TiinyClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.settings = settings
        self.http = http or httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=httpx.Timeout(30.0, read=None),
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
        timeout = httpx.Timeout(30.0, read=LAUNCH_IDLE_TIMEOUT)
        try:
            for event, data in self._sse("POST", path, timeout=timeout):
                yield _launch_event(event, data)
        except httpx.ReadTimeout as e:
            raise TiinyError(
                f"No progress from the device for {LAUNCH_IDLE_TIMEOUT:.0f}s while loading {model_id}; "
                "it may have dropped the request. Check `tai status` and retry."
            ) from e

    def stop(self, model_id: str) -> dict:
        return self._json("POST", f"/api/v1/models/{model_id}/stop")

    # -- device ----------------------------------------------------------
    def sys_status(self) -> dict:
        """CPU, memory, NPU and disk utilisation."""
        return self._json("GET", "/api/v1/sys/status")

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
        body = {"model": model, "messages": messages, "stream": True, **extra}
        for _event, data in self._sse("POST", "/v1/chat/completions", json=body):
            if data.strip() == "[DONE]":
                return
            yield json.loads(data)

    def embed(self, model: str, inputs: list[str]) -> dict:
        return self._json("POST", "/v1/embeddings", json={"model": model, "input": inputs})

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

    def asr(self, model: str, path: Path, language: str | None = None) -> dict:
        data = {"model": model}
        if language:
            data["language"] = language
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
        r = self.http.get(f"/v1/3d/tasks/{task_id}/artifacts/{artifact_id}")
        if r.status_code >= 400:
            raise _err(r)
        return r.content
