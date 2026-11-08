"""Mesh3D pipeline against a scripted fake device."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from tai.client import TiinyError
from tai.mesh import Mesh3D, MeshOptions

ARTIFACTS = {
    "shape": [{"artifact_id": "S", "artifact_type": "shape", "filename": "s.zip"},
              {"artifact_id": "S:preview", "artifact_type": "shape_preview", "filename": "s.glb"}],
    "texture": [{"artifact_id": "M", "artifact_type": "material", "filename": "t.zip"}],
    "simplify": [{"artifact_id": "G", "artifact_type": "geometry", "filename": "g.zip"}],
    "export": [{"artifact_id": "GLB", "artifact_type": "mesh_glb", "filename": "out.glb"}],
}


class FakeClient:
    def __init__(self, polls=None):
        self.submits: list[tuple[str, dict]] = []
        self.polls = polls or {}  # stage -> list of poll results (dict or Exception) before completion

    def task3d_submit(self, stage, **kw):
        self.submits.append((stage, kw))
        return f"t-{stage}"

    def task3d(self, task_id):
        stage = task_id.removeprefix("t-")
        script = self.polls.get(stage)
        if script:
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {"status": "completed", "progress": 100, "message": "done",
                "result": {"output": {"artifacts": ARTIFACTS[stage]}}}

    def task3d_artifact(self, task_id, artifact_id):
        return f"{task_id}/{artifact_id}".encode()


@contextmanager
def no_progress(stage):
    yield lambda pct, msg: None


def pipeline(client, sleeps=None):
    return Mesh3D(client, no_progress, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_full_pipeline_chains_artifact_refs():
    c = FakeClient()
    glb = pipeline(c).generate(Path("img.png"), "out.glb", MeshOptions(quality="fast", seed=7))
    assert glb == b"t-export/GLB"
    stages = {stage: kw for stage, kw in c.submits}
    assert list(stages) == ["shape", "texture", "export"]
    assert stages["shape"] == {"image": Path("img.png"), "params": {"inference_quality": "fast", "seed": 7}}
    shape_ref = {"task_id": "t-shape", "artifact_id": "S"}
    assert stages["texture"]["input_refs"] == {"shape_artifact": shape_ref}
    assert stages["export"]["input_refs"] == {
        "shape_artifact": shape_ref, "material_artifact": {"task_id": "t-texture", "artifact_id": "M"}}
    assert stages["export"]["params"] == {"output_name": "out.glb"}


def test_faces_adds_simplify_and_geometry_ref():
    c = FakeClient()
    pipeline(c).generate(Path("img.png"), "out.glb", MeshOptions(faces=200_000))
    stages = {stage: kw for stage, kw in c.submits}
    assert list(stages) == ["shape", "texture", "simplify", "export"]
    assert stages["simplify"]["params"] == {"simplify": True, "target_faces": 200_000}
    assert stages["export"]["input_refs"]["geometry_artifact"] == {"task_id": "t-simplify", "artifact_id": "G"}
    assert stages["shape"]["params"] is None  # no controls given


def test_polls_until_done_and_retries_gateway_5xx():
    running = {"status": "running", "progress": 40, "message": "working"}
    c = FakeClient(polls={"shape": [running, TiinyError("HTTP 504 GET /v1/3d/tasks/x: timed out"), running]})
    sleeps = []
    result = pipeline(c, sleeps).run("shape", image=Path("img.png"))
    assert result.ref("shape") == {"task_id": "t-shape", "artifact_id": "S"}
    assert sleeps == [3, 5, 3]


def test_client_errors_are_not_retried():
    c = FakeClient(polls={"shape": [TiinyError("HTTP 404 GET /v1/3d/tasks/x: gone")]})
    with pytest.raises(TiinyError, match="404"):
        pipeline(c).run("shape")


def test_failed_stage_raises():
    c = FakeClient(polls={"texture": [{"status": "failed", "error": "OOM"}]})
    with pytest.raises(TiinyError, match="3D texture failed: OOM"):
        pipeline(c).run("texture")


def test_missing_artifact_is_a_clear_error():
    c = FakeClient()
    result = pipeline(c).run("texture")
    with pytest.raises(TiinyError, match="no 'shape' artifact"):
        result.ref("shape")
