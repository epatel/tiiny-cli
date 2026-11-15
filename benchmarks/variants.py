"""Variants: how the agent is run on a task (mode, extra instructions, thinking per stage).

A variant is a JSON file in ``variants/`` (or any path):
    {"mode": "plain" | "checklist" | "phased",
     "prompt": "added to the system prompt",
     "think": {"plan": true, "default": false},   # per stage: a phase name, "work" or "review"
     "stop_after": "plan"}                         # phased: end after this phase (to test the early phases)
The file name is the variant's name, and results are grouped by it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

VARIANTS_DIR = Path(__file__).resolve().parent / "variants"
MODES = ("plain", "checklist", "phased")


@dataclass
class Variant:
    name: str
    mode: str = "plain"
    prompt: str = ""
    think: dict[str, bool] = field(default_factory=dict)
    stop_after: str = ""

    @classmethod
    def load(cls, spec: str) -> Variant:
        """A variant by name (``variants/<name>.json``) or by path."""
        path = Path(spec) if spec.endswith(".json") else VARIANTS_DIR / f"{spec}.json"
        if not path.is_file():
            have = ", ".join(sorted(p.stem for p in VARIANTS_DIR.glob("*.json")))
            raise SystemExit(f"unknown variant {spec!r}; have: {have}")
        data = json.loads(path.read_text())
        if data.get("mode", "plain") not in MODES:
            raise SystemExit(f"{path}: mode must be one of {', '.join(MODES)}")
        return cls(path.stem, data.get("mode", "plain"), data.get("prompt", ""), data.get("think", {}),
                   data.get("stop_after", ""))

    def extra(self, base: dict, stage: str) -> dict:
        """The request extras for ``stage``: thinking switched on or off if the variant says so."""
        on = self.think.get(stage, self.think.get("default"))
        if on is None:
            return base
        return {**base, "chat_template_kwargs": {**base.get("chat_template_kwargs", {}), "enable_thinking": on}}
