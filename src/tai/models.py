"""Model catalog: name resolution, task mapping and NPU swap planning.

The device only serves *loaded* models, and loaded models share an NPU budget
(npu_total, 100). Each model has a fixed ``npu_usage``. To use a model that
doesn't fit we unload others — least-recently-used first, never one with
active requests. See cards/npu-swapping.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    if not query and default:
        query = default
    if not query:
        if not pool:
            raise ResolveError(f"No installed model can do '{task}'.")
        loaded = [m for m in pool if m.loaded]
        return min(loaded or pool, key=lambda m: m.npu)

    q = query.lower()
    for match in (
        lambda m: m.id.lower() == q,
        lambda m: m.name.lower() == q,
        lambda m: q in m.id.lower(),
    ):
        hits = [m for m in models if match(m)]
        if len(hits) == 1:
            m = hits[0]
            if task and m.task != task:
                raise ResolveError(f"{m.name} is a {m.type} model, not usable for '{task}'.")
            return m
        if len(hits) > 1:
            raise ResolveError(f"'{query}' is ambiguous: {', '.join(h.name for h in hits)}")
    names = ", ".join(m.name for m in pool or models)
    raise ResolveError(f"No model matches '{query}'. Installed: {names}")


@dataclass
class SwapPlan:
    target: Model
    unload: list[Model]
    free_before: int
    total: int

    @property
    def needed(self) -> bool:
        return not self.target.loaded


def plan_swap(models: list[Model], target: Model, total: int = 100) -> SwapPlan:
    """Decide which loaded models to unload so ``target`` fits in the NPU budget."""
    used = sum(m.npu for m in models if m.loaded or m.status in LOADING)
    free = total - used
    if target.loaded:
        return SwapPlan(target, [], free, total)
    if target.npu > total:
        raise ResolveError(f"{target.name} needs {target.npu}% NPU, more than the device has.")
    victims: list[Model] = []
    candidates = sorted(
        (m for m in models if m.loaded and m.id != target.id and m.active == 0),
        key=lambda m: m.last_used,
    )
    for m in candidates:
        if free >= target.npu:
            break
        victims.append(m)
        free += m.npu
    if free < target.npu:
        busy = ", ".join(m.name for m in models if m.loaded and m.active)
        raise ResolveError(
            f"Cannot free {target.npu}% NPU for {target.name}"
            + (f"; busy models: {busy}" if busy else "")
        )
    return SwapPlan(target, victims, total - used, total)
