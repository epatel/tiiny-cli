"""Optional model judge: scores a run's output files against the task's rubric (1-10 per criterion).

The judge is the device's own model, so it shares the writer's blind spots; use it to compare
plain against phased, not as an absolute measure. Files are shown without saying which mode made them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MAX_FILE_CHARS = 24_000

PROMPT = """You are a strict, fair reviewer. Score the work below against each criterion from 1 (very poor)
to 10 (excellent). Use the whole scale: 5 is mediocre, 8 is strong, 10 is rare.

# The task
{task}

# Criteria
{criteria}

# The work
{files}

Answer with JSON only, no other text:
{{"scores": {{"<criterion number>": <score>, ...}}, "comment": "<two sentences on the main strengths and weaknesses>"}}"""


def judge(setup, task, repo: Path) -> dict:
    """Score ``repo``'s files with ``setup``'s client and model (a harness.Setup).
    Returns {"scores": {criterion: n}, "mean": x, "comment": ...}, or {"error": ...}."""
    spec = task.judge or {}
    criteria = list(spec.get("rubric", []))
    shown = [_file(repo, name) for name in spec.get("files", [])]
    prompt = PROMPT.format(task=task.prompt, criteria="\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1)),
                           files="\n\n".join(shown))
    messages = [{"role": "user", "content": prompt}]
    stream = setup.client.chat_stream(setup.model_id, messages, **setup.extra)
    text = "".join(_content(chunk) for chunk in stream)
    return parse(text, criteria)


def _file(repo: Path, name: str) -> str:
    p = repo / name
    body = p.read_text(errors="replace")[:MAX_FILE_CHARS] if p.is_file() else "(missing)"
    return f"## {name}\n\n{body}"


def _content(chunk: dict) -> str:
    choices = chunk.get("choices") or [{}]
    return (choices[0].get("delta") or {}).get("content") or ""


def parse(text: str, criteria: list[str]) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
        raw = data.get("scores", {})
        scores = {c: max(1, min(10, int(raw[str(i)]))) for i, c in enumerate(criteria, 1)}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {"error": "the judge's answer could not be read", "raw": text[-500:]}
    return {"scores": scores, "mean": round(sum(scores.values()) / len(scores), 2),
            "comment": str(data.get("comment", ""))}
