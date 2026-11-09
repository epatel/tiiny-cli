"""`tai info`: what a model is (catalog row) and what it reports about itself.

Only some models describe their options, and only while loaded (checked on the device
2026-11-09): chat and TTS answer `GET /v1/capabilities?model=`, 3D models answer
`GET /v1/3d/capabilities`, video models `GET /v1/video/info?model=`. Image, ASR and
embedding models have no such endpoint
(the gateway answers "does not support tts"), so for them the catalog row is all there is.
"""

from __future__ import annotations

from typing import Callable

from tai.client import TiinyClient
from tai.models import Model

# task -> how to ask a loaded model for its options
REPORTERS: dict[str, Callable[[TiinyClient, str], dict]] = {
    "chat": lambda c, model_id: c.capabilities(model_id),
    "tts": lambda c, model_id: c.capabilities(model_id),
    "3d": lambda c, model_id: c.task3d_capabilities(model_id),
    "video": lambda c, model_id: c.video_info(model_id),
}

# keys that repeat the catalog row or say nothing useful
_SKIP = {"object", "model", "type"}


def reports_options(m: Model) -> bool:
    return m.task in REPORTERS


def catalog_row(m: Model) -> dict:
    row = {"id": m.id, "name": m.name, "type": m.type, "task": m.task, "npu": f"{m.npu}%",
           "status": m.status, "inputs": m.inputs, "supported": m.supported}
    if m.thinking.get("supported"):
        row["thinking"] = m.thinking
    return {k: v for k, v in row.items() if v not in ([], "", None)}


def reported(client: TiinyClient, m: Model) -> dict:
    """The model's own description; the model must be loaded."""
    data = REPORTERS[m.task](client, m.id)
    return {k: v for k, v in data.items() if k not in _SKIP}


def render(data: dict, indent: int = 0) -> list[str]:
    """Nested dicts/lists as indented `key: value` lines; scalar lists on one line."""
    lines: list[str] = []
    for key, value in data.items():
        lines += _entry(key, value, indent)
    return lines


def _entry(key, value, indent: int) -> list[str]:
    pad = "  " * indent
    if isinstance(value, dict):
        return [f"{pad}{key}:"] + (render(value, indent + 1) or [f"{pad}  (none)"])
    if isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value):
        items = {str(i): item for i, item in enumerate(value, 1)}
        return [f"{pad}{key}:"] + render(items, indent + 1)
    if isinstance(value, list):
        return [f"{pad}{key}: {', '.join(map(str, value)) or '(none)'}"]
    return [f"{pad}{key}: {value}"]
