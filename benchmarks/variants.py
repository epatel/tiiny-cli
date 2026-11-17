"""Variants: how the agent is run on a task (mode, extra instructions, thinking per stage).

A variant is a JSON file in ``variants/`` (or any path):
    {"mode": "plain" | "checklist" | "phased",
     "extends": "lean",                             # start from another variant (its prompts come first)
     "prompt": "added to the system prompt",
     "prompt_by_domain": {"python": "...", "typescript": "..."},  # added for tasks of that domain
     "think": {"plan": true, "default": false},   # per stage: a phase name, "work" or "review"
     "stop_after": "plan",                         # phased: end after this phase (to test the early phases)
     "style": false,                               # leave out tai agent's working style (prompt.STYLE)
     "nudges": false}                              # turn off the loop's nudges (tai.agent.nudges)
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
    prompt_by_domain: dict[str, str] = field(default_factory=dict)
    style: bool = True
    nudges: bool = True

    @classmethod
    def load(cls, spec: str) -> Variant:
        """A variant by name (``variants/<name>.json``) or by path."""
        path = Path(spec) if spec.endswith(".json") else VARIANTS_DIR / f"{spec}.json"
        if not path.is_file():
            have = ", ".join(sorted(p.stem for p in VARIANTS_DIR.glob("*.json")))
            raise SystemExit(f"unknown variant {spec!r}; have: {have}")
        data = json.loads(path.read_text())
        base = cls.load(data["extends"]) if data.get("extends") else cls(path.stem)
        mode = data.get("mode", base.mode)
        if mode not in MODES:
            raise SystemExit(f"{path}: mode must be one of {', '.join(MODES)}")
        by_domain = {d: base.prompt_by_domain.get(d, "") + data.get("prompt_by_domain", {}).get(d, "")
                     for d in {*base.prompt_by_domain, *data.get("prompt_by_domain", {})}}
        return cls(path.stem, mode, base.prompt + data.get("prompt", ""), {**base.think, **data.get("think", {})},
                   data.get("stop_after", base.stop_after), by_domain, style=data.get("style", base.style),
                   nudges=data.get("nudges", base.nudges))

    def prompt_for(self, domain: str) -> str:
        """What the variant adds to the system prompt for a task of ``domain``."""
        return self.prompt + self.prompt_by_domain.get(domain, "")

    def extra(self, base: dict, stage: str) -> dict:
        """The request extras for ``stage``: thinking switched on or off if the variant says so."""
        on = self.think.get(stage, self.think.get("default"))
        if on is None:
            return base
        return {**base, "chat_template_kwargs": {**base.get("chat_template_kwargs", {}), "enable_thinking": on}}
