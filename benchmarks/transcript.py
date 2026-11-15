"""A chat client wrapper that counts usage and writes every request of a run to ``requests.jsonl``.

Each line is one model call: the messages added since the previous call (``fresh`` when the
conversation was replaced, e.g. a new phase or a compaction), what the model wrote back, its token
counts and the device's llama.cpp ``timings``. ``output_by`` totals the written tokens per kind of
reply (a tool name, or ``text``), which shows where a run's time went: writing is the slow part.
"""

from __future__ import annotations

import copy
import json
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


class Reply:
    """What the model streamed back in one request."""

    def __init__(self):
        self.text: list[str] = []
        self.calls: dict[int, dict] = {}
        self.usage: dict = {}
        self.timings: dict = {}

    def add(self, chunk: dict) -> None:
        self.usage = chunk.get("usage") or self.usage
        self.timings = chunk.get("timings") or self.timings
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            self.text.append(delta.get("content") or "")
            for call in delta.get("tool_calls") or []:
                self._add_call(call)

    def _add_call(self, call: dict) -> None:
        mine = self.calls.setdefault(call.get("index", 0), {"name": "", "arguments": ""})
        fn = call.get("function") or {}
        mine["name"] += fn.get("name") or ""
        mine["arguments"] += fn.get("arguments") or ""

    @property
    def kind(self) -> str:
        """The first tool called, or ``text`` for a plain answer."""
        names = [c["name"] for c in self.calls.values() if c["name"]]
        return names[0] if names else "text"

    def to_dict(self) -> dict:
        return {"text": "".join(self.text), "tool_calls": list(self.calls.values())}


@dataclass
class Request:
    """One model call: the messages new since the previous call, and the reply."""
    n: int
    fresh: bool  # the conversation was replaced (a new phase, a compaction): ``messages`` is all of it
    messages: list[dict]
    reply: Reply = field(default_factory=Reply)
    seconds: float = 0.0

    def to_json(self) -> str:
        r = self.reply
        return json.dumps({"n": self.n, "fresh": self.fresh, "messages": self.messages, "reply": r.to_dict(),
                           "kind": r.kind, "usage": r.usage, "timings": r.timings, "seconds": round(self.seconds, 2)},
                          ensure_ascii=False)


class MeteredClient:
    """Wraps a chat client: counts requests and tokens, and logs each request to ``path`` (if given)."""

    def __init__(self, client, path: Path | None = None):
        self.client, self.path = client, path
        self.requests = self.prompt_tokens = self.completion_tokens = 0
        self.output_by: Counter[str] = Counter()
        self.broken = 0  # requests that ended without usage: cut off or an empty error reply
        self.predicted_n = 0
        self.predicted_ms = 0.0
        self._sent: list[dict] = []

    @property
    def write_speed(self) -> float:
        """Tokens written per second while writing (from the device's timings; 0 if it sent none)."""
        return round(self.predicted_n / self.predicted_ms * 1000, 1) if self.predicted_ms else 0.0

    def chat_stream(self, model: str, messages: list[dict], **extra) -> Iterator[dict]:
        self.requests += 1
        req, start = self._new_request(messages), time.monotonic()
        try:
            for chunk in self.client.chat_stream(model, messages, **extra):
                req.reply.add(chunk)
                yield chunk
        finally:  # also when the turn is cancelled mid-stream
            req.seconds = time.monotonic() - start
            self._count(req.reply)
            self._write(req)

    def _new_request(self, messages: list[dict]) -> Request:
        prev = self._sent
        fresh = not prev or messages[:len(prev)] != prev
        self._sent = copy.deepcopy(messages)
        return Request(self.requests, fresh, messages if fresh else messages[len(prev):])

    def _count(self, reply: Reply) -> None:
        self.broken += not reply.usage
        written = reply.usage.get("completion_tokens") or 0
        self.prompt_tokens += reply.usage.get("prompt_tokens") or 0
        self.completion_tokens += written
        self.output_by[reply.kind] += written
        self.predicted_n += reply.timings.get("predicted_n") or 0
        self.predicted_ms += reply.timings.get("predicted_ms") or 0.0

    def _write(self, req: Request) -> None:
        if self.path:
            with open(self.path, "a") as f:
                f.write(req.to_json() + "\n")
