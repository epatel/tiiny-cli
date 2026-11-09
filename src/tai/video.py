"""Text- or image-to-video on MiniMax-H3-Turbo: submit one job, poll it, download the MP4.

The model is compiled for a fixed set of sizes (`canvases` in `/v1/video/info`) and at
most 7 seconds; the device accepts other values at submit time and fails later, so they
are checked here first. Renders take minutes, and the gateway gives up after one hour
while the NPU keeps rendering. See the video section of cards/device-api.md.
"""

from __future__ import annotations

# Works on the device's wire format: JSON objects, string ids and "WxH" sizes.
# aihealth: disable=primitive_obsession

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from tai.chat import data_url
from tai.client import TiinyClient, TiinyError
from tai.options import merge

MAX_SECONDS = 7
FAILED = ("failed", "cancelled", "error")

# label -> context manager yielding update(progress_percent, message)
ProgressFactory = Callable[[str], AbstractContextManager[Callable[[float, str], None]]]


@dataclass(frozen=True)
class VideoOptions:
    size: str = "832x480"
    seconds: int = 4
    steps: int | None = None
    seed: int | None = None
    image: Path | None = None  # start frame
    extra: dict = field(default_factory=dict)  # -x fields, over the ones above

    def request(self, seed: int) -> dict:
        fields = {"size": self.size, "seconds": str(self.seconds), "steps": self.steps, "seed": seed,
                  "image": data_url(self.image) if self.image else None}
        return merge({k: v for k, v in fields.items() if v is not None}, self.extra)

    def problems(self, info: dict) -> list[str]:
        """What the model would reject, judged by its `/v1/video/info`."""
        found = []
        if not 1 <= self.seconds <= MAX_SECONDS:
            found.append(f"--seconds must be 1-{MAX_SECONDS} (the model can't render longer clips)")
        canvases = sizes(info)
        if _outside(self.size, canvases):
            found.append(f"--size must be one of {', '.join(canvases)}")
        steps = info.get("step_options") or []
        if _outside(self.steps, steps):
            found.append(f"--steps must be one of {', '.join(map(str, steps))}")
        return found


def sizes(info: dict) -> list[str]:
    """The model's `canvases` as "WxH" (the device lists them as {width, height})."""
    return [f"{c['width']}x{c['height']}" if isinstance(c, dict) else str(c)
            for c in info.get("canvases") or []]


def _outside(value, allowed: list) -> bool:
    """``value`` was given and isn't allowed. An empty list means the model didn't say."""
    return value is not None and value not in (allowed or [value])


class VideoJob:
    def __init__(self, client: TiinyClient, progress: ProgressFactory,
                 sleep: Callable[[float], None] | None = None):
        self.client, self.progress, self.sleep = client, progress, sleep or time.sleep

    def render(self, model: str, prompt: str, request: dict) -> bytes:
        """Submit, poll to completion and return the MP4 bytes."""
        video_id = self.client.video_submit(model, prompt, **request)
        with self.progress("render") as update:
            while True:
                job = self._poll(video_id)
                update(job.get("progress") or 0, job.get("status") or "")
                if job.get("status") == "completed":
                    return self.client.video_content(video_id)
                if job.get("status") in FAILED:
                    raise TiinyError(f"video {job['status']}: {_reason(job.get('error'))}")
                self.sleep(5)

    def _poll(self, video_id: str) -> dict:
        """Job status; retries 5xx, which the gateway returns while the device is busy."""
        while True:
            try:
                return self.client.video(video_id)
            except TiinyError as e:
                if "HTTP 5" not in str(e):
                    raise
                self.sleep(5)


def _reason(error) -> str:
    if isinstance(error, dict):
        return error.get("message") or error.get("code") or str(error)
    return str(error)
