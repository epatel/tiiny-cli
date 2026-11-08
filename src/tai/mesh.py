"""Image-to-3D pipeline on the TRELLIS model: shape -> texture -> (simplify) -> export.

Each stage is a device task that is submitted, polled until done, and yields
artifacts that later stages reference by {task_id, artifact_id}. See the 3D
section of cards/device-api.md for the wire format.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from tai.client import TiinyClient, TiinyError

FAILED = ("failed", "cancelled", "error")

# stage name -> context manager yielding update(progress_percent, message)
ProgressFactory = Callable[[str], AbstractContextManager[Callable[[float, str], None]]]


@dataclass(frozen=True)
class MeshOptions:
    faces: int | None = None  # run the simplify stage down to about this many faces
    quality: str | None = None  # fast | balance | quality
    seed: int | None = None

    @property
    def controls(self) -> dict | None:
        c = {"inference_quality": self.quality, "seed": self.seed}
        return {k: v for k, v in c.items() if v is not None} or None

    def controls_for(self, spec) -> dict | None:
        """The controls a stage accepts. TRELLIS rejects params a stage doesn't list in
        its `stage_specs` entry (params + controls); without a spec, send them all."""
        if not self.controls or not isinstance(spec, dict):
            return self.controls
        allowed = set(spec.get("params") or {}) | set(spec.get("controls") or [])
        return {k: v for k, v in self.controls.items() if k in allowed} or None


@dataclass(frozen=True)
class StageResult:
    task_id: str
    artifacts: list[dict]

    def find(self, match: Callable[[dict], bool]) -> dict | None:
        return next((a for a in self.artifacts if match(a)), None)

    def ref(self, kind: str) -> dict:
        """Input reference to this stage's artifact of type ``kind`` for a later stage."""
        art = self.find(lambda a: a["artifact_type"] == kind)
        if art is None:
            raise TiinyError(f"3D stage produced no '{kind}' artifact")
        return {"task_id": self.task_id, "artifact_id": art["artifact_id"]}


class Mesh3D:
    def __init__(self, client: TiinyClient, progress: ProgressFactory,
                 sleep: Callable[[float], None] = time.sleep):
        self.client, self.progress, self.sleep = client, progress, sleep

    def generate(self, image: Path, output_name: str, opts: MeshOptions) -> bytes:
        """Run the whole pipeline and return the .glb bytes."""
        controls = self._stage_controls(opts)
        shape = self.run("shape", image=image, params=controls["shape"])
        shape_ref = {"shape_artifact": shape.ref("shape")}
        texture = self.run("texture", input_refs=shape_ref, params=controls["texture"])
        refs = {**shape_ref, "material_artifact": texture.ref("material")}
        if opts.faces:
            refs.update(self._simplify(shape_ref, opts.faces))
        export = self.run("export", input_refs=refs, params={"output_name": output_name})
        glb = export.find(lambda a: a["filename"].endswith(".glb"))
        if glb is None:
            raise TiinyError("3D export produced no .glb")
        return self.client.task3d_artifact(export.task_id, glb["artifact_id"])

    def _stage_controls(self, opts: MeshOptions) -> dict:
        """Controls per stage (shape, texture), filtered by the model's `stage_specs`."""
        specs = (self.client.task3d_capabilities().get("stage_specs") or {}) if opts.controls else {}
        return {stage: opts.controls_for(specs.get(stage)) for stage in ("shape", "texture")}

    def _simplify(self, shape_ref: dict, faces: int) -> dict:
        result = self.run("simplify", input_refs=shape_ref,
                          params={"simplify": True, "target_faces": faces})
        geo = result.find(lambda a: a["artifact_type"].startswith("geometry"))
        if geo is None:
            return {}
        return {"geometry_artifact": {"task_id": result.task_id, "artifact_id": geo["artifact_id"]}}

    def run(self, stage: str, **submit) -> StageResult:
        """Submit one stage and poll it to completion."""
        task_id = self.client.task3d_submit(stage, **submit)
        with self.progress(stage) as update:
            while True:
                task = self._poll(task_id)
                update(task.get("progress") or 0, task.get("message") or "")
                if task["status"] == "completed":
                    return StageResult(task_id, task["result"]["output"]["artifacts"])
                if task["status"] in FAILED:
                    raise TiinyError(f"3D {stage} {task['status']}: {task.get('error')}")
                self.sleep(3)

    def _poll(self, task_id: str) -> dict:
        """Task status; retries 5xx, which the gateway returns while the device is busy."""
        while True:
            try:
                return self.client.task3d(task_id)
            except TiinyError as e:
                if "HTTP 5" not in str(e):
                    raise
                self.sleep(5)
