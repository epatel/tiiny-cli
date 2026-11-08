"""Model catalog: name resolution, task mapping and NPU swap planning.

The device only serves *loaded* models, and loaded models share an NPU budget
(npu_total, 100). Each model has a fixed ``npu_usage``. To use a model that
doesn't fit we unload the idle models that free the least NPU while still making
room (ties: fewer models, then least recently used), never one with active requests. See cards/npu-swapping.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

# Device "type" strings -> tai task names.
TASK_BY_TYPE = {
    "Text Generation": "chat",
    "Image-Text-to-Text": "chat",
    "Text-to-Image": "image",
    "Text-to-Speech": "tts",
    "ASR": "asr",
    "Text Embedding": "embed",
    "Image-to-3D": "3d",
}

LOADED = ("running",)
LOADING = ("pending", "starting", "loading", "launching")


@dataclass
class Model:
    id: str
    name: str
    type: str
    npu: int
    status: str
    inputs: list[str] = field(default_factory=list)
    thinking: dict = field(default_factory=dict)
    last_used: float = 0.0  # from running instances (use_time); 0 if unknown
    active: int = 0  # active_request_count

    @property
    def task(self) -> str:
        return TASK_BY_TYPE.get(self.type, self.type.lower())

    @property
    def loaded(self) -> bool:
        return self.status in LOADED

    @property
    def idle(self) -> bool:
        """Loaded and serving no requests, so safe to unload."""
        return self.loaded and self.active == 0

    @property
    def vision(self) -> bool:
        return "Image" in self.inputs

    @classmethod
    def from_api(cls, d: dict) -> Model:
        return cls(
            id=d["id"],
            name=d.get("display_name") or d.get("name") or d["id"].split("/")[-1],
            type=d.get("type", ""),
            npu=int(d.get("npu_usage") or 0),
            status=d.get("status", ""),
            inputs=[s.strip() for s in (d.get("input") or "").split(",") if s.strip()],
            thinking=d.get("thinking") or {},
        )


class ResolveError(LookupError):
    pass


def build_catalog(models_json: list[dict], running_json: dict | None = None) -> list[Model]:
    models = [Model.from_api(d) for d in models_json]
    if running_json:
        inst = {i["model_id"]: i for i in running_json.get("instances", {}).get("running", [])}
        for m in models:
            i = inst.get(m.id)
            if i:
                m.status = i.get("status", m.status)
                m.last_used = float(i.get("last_inference_time") or i.get("use_time") or 0)
                m.active = int(i.get("active_request_count") or 0)
    return models


def resolve(models: list[Model], query: str | None, task: str | None = None,
            default: str | None = None) -> Model:
    """Find a model by full id, display name, or unique case-insensitive substring.

    With no query: use ``default`` (a stored per-task preference) if given, else
    prefer a loaded model for ``task``, else the one with the smallest NPU cost.
    """
    pool = [m for m in models if task is None or m.task == task]
    query = query or default
    if not query:
        return _pick_default(pool, task)
    model = _match(models, query, pool)
    if task and model.task != task:
        raise ResolveError(f"{model.name} is a {model.type} model, not usable for '{task}'.")
    return model


def _pick_default(pool: list[Model], task: str | None) -> Model:
    if not pool:
        raise ResolveError(f"No installed model can do '{task}'.")
    loaded = [m for m in pool if m.loaded]
    return min(loaded or pool, key=lambda m: m.npu)


def _match(models: list[Model], query: str, pool: list[Model]) -> Model:
    """Try exact id, then exact name, then id substring; the first unique hit wins."""
    q = query.lower()
    rules = (
        lambda m: m.id.lower() == q,
        lambda m: m.name.lower() == q,
        lambda m: q in m.id.lower(),
    )
    for rule in rules:
        hits = [m for m in models if rule(m)]
        if len(hits) > 1:
            raise ResolveError(f"'{query}' is ambiguous: {', '.join(h.name for h in hits)}")
        if hits:
            return hits[0]
    names = ", ".join(m.name for m in pool or models)
    raise ResolveError(f"No model matches '{query}'. Installed: {names}")


@dataclass
class SwapPlan:
    target: Model
    unload: list[Model]
    free_before: int


def plan_swap(models: list[Model], target: Model, total: int = 100) -> SwapPlan:
    """Decide which loaded models to unload so ``target`` fits in the NPU budget."""
    free = total - sum(m.npu for m in models if m.loaded or m.status in LOADING)
    if target.loaded:
        return SwapPlan(target, [], free)
    if target.npu > total:
        raise ResolveError(f"{target.name} needs {target.npu}% NPU, more than the device has.")
    victims = _cheapest_victims(models, target, free)
    if free + sum(m.npu for m in victims) < target.npu:
        raise ResolveError(_cannot_free_message(models, target))
    return SwapPlan(target, victims, free)


MAX_EXACT = 16  # idle models searched exhaustively; beyond that, greedy smallest-first


def _cheapest_victims(models: list[Model], target: Model, free: int) -> list[Model]:
    """Idle models whose unloading frees the least NPU while making ``target`` fit.

    Keeping big, expensive-to-reload models loaded matters more than recency, so
    cost is the NPU freed; ties prefer fewer models, then the least recently used.
    """
    need = target.npu - free
    if need <= 0:
        return []
    idle = sorted((m for m in models if m.idle and m.id != target.id), key=lambda m: m.last_used)
    return _greedy_victims(idle, need) if len(idle) > MAX_EXACT else _exact_victims(idle, need)


def _exact_victims(idle: list[Model], need: int) -> list[Model]:
    """Try every combination (the device holds a handful of models)."""
    subsets = (c for size in range(1, len(idle) + 1) for c in combinations(idle, size))
    feasible = (c for c in subsets if sum(m.npu for m in c) >= need)
    return list(min(feasible, key=_swap_cost, default=()))


def _swap_cost(combo: tuple[Model, ...]) -> tuple:
    return sum(m.npu for m in combo), len(combo), max(m.last_used for m in combo)


def _greedy_victims(idle: list[Model], need: int) -> list[Model]:
    victims: list[Model] = []
    for m in sorted(idle, key=lambda m: (m.npu, m.last_used)):
        if need <= 0:
            break
        victims.append(m)
        need -= m.npu
    return victims


def _cannot_free_message(models: list[Model], target: Model) -> str:
    busy = ", ".join(m.name for m in models if m.loaded and m.active)
    return f"Cannot free {target.npu}% NPU for {target.name}" + (f"; busy models: {busy}" if busy else "")
