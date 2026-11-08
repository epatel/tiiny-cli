"""App.ensure_loaded against a fake device: swap planning, confirmation, fragmentation retry."""

import click
import pytest

from tai.cli import App, LoadFailed

OOM = {"_event": "done", "success": False,
       "startup_error": "Container exited; Cannot allocate memory", "toast": {"message": "unload all"}}
OK = [{"_event": "progress", "progress": 50}, {"_event": "done", "progress": 100, "success": True}]


class FakeClient:
    def __init__(self, models, launches=None):
        self._models = {m["id"]: m for m in models}
        self.launches = list(launches or [])  # one event list per launch call
        self.log: list[tuple[str, str]] = []

    def models(self):
        return list(self._models.values())

    def running(self):
        return {"instances": {"running": [
            {"model_id": m["id"], "status": "running", "use_time": m.get("used", 0)}
            for m in self._models.values() if m["status"] == "running"]}}

    def npu(self):
        return {"npu_total": 100}

    def stop(self, model_id):
        self.log.append(("stop", model_id))
        self._models[model_id]["status"] = "downloaded"

    def launch(self, model_id):
        self.log.append(("launch", model_id))
        events = self.launches.pop(0) if self.launches else OK
        if events[-1].get("success") is not False:
            self._models[model_id]["status"] = "running"
        yield from events


def m(id, npu, status="downloaded", used=0):
    return {"id": id, "display_name": id, "type": "ASR", "npu_usage": npu, "status": status, "used": used}


def app_with(client, yes=True):
    app = App(device=None, yes=yes)
    app._client = client
    return app


def target(app, id):
    return next(x for x in app.catalog() if x.id == id)


def test_already_loaded_does_nothing():
    c = FakeClient([m("a", 10, "running")])
    app = app_with(c)
    app.ensure_loaded(target(app, "a"))
    assert c.log == []


def test_fits_without_unloading():
    c = FakeClient([m("a", 10, "running"), m("b", 20)])
    app = app_with(c)
    app.ensure_loaded(target(app, "b"))
    assert c.log == [("launch", "b")]


def test_unloads_lru_then_loads():
    c = FakeClient([m("old", 40, "running", used=1), m("new", 40, "running", used=2), m("big", 50)])
    app = app_with(c)
    app.ensure_loaded(target(app, "big"))
    assert c.log == [("stop", "old"), ("launch", "big")]


def test_declining_swap_aborts(monkeypatch):
    c = FakeClient([m("a", 60, "running"), m("big", 50)])
    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
    app = app_with(c, yes=False)
    with pytest.raises(click.Abort):
        app.ensure_loaded(target(app, "big"))
    assert c.log == []


def test_memory_failure_unloads_everything_and_retries():
    c = FakeClient([m("small", 3, "running"), m("big", 94)], launches=[[OOM], OK])
    app = app_with(c)
    app.ensure_loaded(target(app, "big"))
    assert c.log == [("launch", "big"), ("stop", "small"), ("launch", "big")]


def test_memory_failure_with_nothing_to_unload_raises():
    c = FakeClient([m("big", 94)], launches=[[OOM]])
    app = app_with(c)
    with pytest.raises(LoadFailed, match="NPU out of memory"):
        app.ensure_loaded(target(app, "big"))


def test_other_failures_are_not_retried():
    bad = {"_event": "done", "success": False, "startup_error": "image pull failed"}
    c = FakeClient([m("small", 3, "running"), m("big", 50)], launches=[[bad]])
    app = app_with(c)
    with pytest.raises(LoadFailed, match="image pull failed"):
        app.ensure_loaded(target(app, "big"))
    assert c.log == [("launch", "big")]
