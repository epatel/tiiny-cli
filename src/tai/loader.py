"""Loading models on demand, independent of the UI.

``ModelLoader.ensure_loaded`` plans which idle models to unload (see
cards/npu-swapping.md), asks the UI before unloading anything, loads the model
with progress, and retries once after unloading every other idle model when the
NPU reports fragmented memory. The CLI and the agent TUI supply their own
``LoaderUI`` (terminal prompts and progress bars vs. a modal and a status line).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from tai.client import TiinyClient, TiinyError
from tai.models import Model, build_catalog, plan_swap

ProgressUpdate = Callable[[float, str], None]  # (percent, message)


class LoaderUI(Protocol):
    def confirm(self, question: str) -> bool: ...
    def info(self, message: str) -> None: ...
    def progress(self, name: str) -> AbstractContextManager[ProgressUpdate]: ...


class LoadCancelled(Exception):
    """The user declined to unload models."""


class LoadFailed(TiinyError):
    """A model failed to start. ``memory`` is True for NPU allocation failures."""

    def __init__(self, target: Model, ev: dict):
        detail = ev.get("error") or ev.get("startup_error") or ev.get("message") or ""
        self.memory = "allocate memory" in detail or "ALLOC" in detail
        toast = (ev.get("toast") or {}).get("message")
        short = detail.split(";")[0].strip() or "unknown error"
        super().__init__(f"Loading {target.name} failed: {short}"
                         + (" (NPU out of memory)" if self.memory else "")
                         + (f" — device says: {toast}" if toast else ""))


class ModelLoader:
    def __init__(self, client: TiinyClient, ui: LoaderUI, protect: str | None = None):
        """``protect``: a model id to point out when it would be unloaded (e.g. the agent's own)."""
        self.client, self.ui, self.protect = client, ui, protect

    def catalog(self) -> list[Model]:
        return build_catalog(self.client.models(), self.client.running())

    def ensure_loaded(self, target: Model) -> None:
        if target.loaded:
            return
        models = self.catalog()
        target = next(m for m in models if m.id == target.id)  # fresh status
        if target.loaded:
            return
        self._make_room(models, target)
        try:
            self.load(target)
        except LoadFailed as e:
            if not e.memory:
                raise
            self._retry_after_fragmentation(target, e)

    def _make_room(self, models: list[Model], target: Model) -> None:
        plan = plan_swap(models, target, total=int(self.client.npu().get("npu_total") or 100))
        if not plan.unload:
            return
        self._ask(f"{target.name} needs {target.npu}% NPU, {plan.free_before}% free. "
                  f"Unload: {self._names(plan.unload)}?")
        self.unload(plan.unload)

    def _retry_after_fragmentation(self, target: Model, failure: LoadFailed) -> None:
        """NPU memory fragments: a big model may not fit after small ones even when
        the percentages add up. The device's own advice is to unload all and retry."""
        others = [m for m in self.catalog() if m.idle and m.id != target.id]  # never busy ones
        if not others:
            raise failure
        self._ask(f"{failure}\nNPU memory is fragmented. Unload all other models "
                  f"({self._names(others)}) and retry?")
        self.unload(others)
        self.load(target)

    def _names(self, models: list[Model]) -> str:
        return ", ".join(f"{m.name} ({m.npu}%)" + (" — the agent's model" if m.id == self.protect else "")
                         for m in models)

    def _ask(self, question: str) -> None:
        if not self.ui.confirm(question):
            raise LoadCancelled(question)

    def unload(self, models: list[Model]) -> None:
        for m in models:
            self.ui.info(f"Unloading {m.name}…")
            self.client.stop(m.id)

    def load(self, target: Model) -> None:
        last: dict = {}
        with self.ui.progress(target.name) as update:
            for ev in self.client.launch(target.id):
                last = ev
                update(float(ev.get("progress") or 0), ev.get("startup_message") or ev.get("message") or "")
                if ev.get("error"):
                    raise LoadFailed(target, ev)
        if last.get("_event") == "done" and last.get("success") is False:
            raise LoadFailed(target, last)
        self.ui.info(f"Loaded {target.name}")
