"""`tai info`: catalog row plus what a loaded model reports, asking before a load."""

import json

import pytest
from click.testing import CliRunner

import tai.cli as cli_mod
from tai.config import Settings
from tai.info import render

MODELS = [
    {"id": "k2/Voice", "display_name": "Voice", "type": "Text-to-Speech", "npu_usage": 10,
     "status": "running"},
    {"id": "q/Coder", "display_name": "Coder", "type": "Text Generation", "npu_usage": 50,
     "status": "downloaded", "input": "Text", "supported": ["Tool Use"], "context_window": 131072},
    {"id": "z/Image", "display_name": "Image", "type": "Text-to-Image", "npu_usage": 20,
     "status": "running"},
    {"id": "m/Trellis", "display_name": "Trellis", "type": "Image-to-3D", "npu_usage": 30,
     "status": "running"},
]


class FakeClient:
    def __init__(self):
        self.asked: list[str] = []

    def models(self):
        return MODELS

    def running(self):
        return {"instances": {"running": []}}

    def capabilities(self, model):
        self.asked.append(model)
        return {"object": "capabilities", "model": model,
                "instruct_exclusive_groups": [["male", "female"]], "duration": {"min": 1.28}}

    def task3d_capabilities(self, model=None):
        self.asked.append(model)
        return {"stage_specs": {"shape": {"controls": ["seed"]}}}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeClient()
    loads: list[str] = []
    monkeypatch.setattr(cli_mod, "TiinyClient", lambda settings: client)
    monkeypatch.setattr(cli_mod, "load_settings", lambda **kw: Settings("dev", "tok"))
    monkeypatch.setattr(cli_mod.App, "ensure_loaded", lambda app, m: loads.append(m.id))
    monkeypatch.setenv("TAI_HOME", str(tmp_path / "home"))
    client.loads = loads
    return client


def run(*args, input=None):
    return CliRunner().invoke(cli_mod.cli, list(args), input=input, catch_exceptions=False)


def test_loaded_tts_model_shows_catalog_and_reported_options(fake):
    r = run("info", "voice")
    assert r.exit_code == 0
    assert "catalog" in r.output and "Text-to-Speech" in r.output
    assert "reported" in r.output and "1: male, female" in r.output and "min: 1.28" in r.output
    assert "object" not in r.output  # noise keys dropped
    assert fake.asked == ["k2/Voice"] and fake.loads == []


def test_unloaded_model_asks_before_loading(fake):
    r = run("info", "coder", input="n\n")
    assert "isn't loaded" in r.output and "reported" not in r.output
    assert fake.loads == [] and fake.asked == []

    r = run("info", "coder", input="y\n")
    assert fake.loads == ["q/Coder"] and fake.asked == ["q/Coder"] and "reported" in r.output


def test_context_window_in_info_and_models(fake):
    assert "context_window: 131072" in run("info", "coder", "--no-load").output
    assert "context_window" not in run("info", "voice").output  # not reported: not shown
    assert "131K" in run("models").output


def test_no_load_and_yes(fake):
    r = run("info", "coder", "--no-load")
    assert "Load it" not in r.output and fake.loads == []
    run("-y", "info", "coder")
    assert fake.loads == ["q/Coder"]


def test_model_types_without_options_are_not_queried(fake):
    r = run("info", "image")
    assert "don't report their options" in r.output and fake.asked == []


def test_3d_and_json(fake):
    r = run("info", "trellis", "--json")
    data = json.loads(r.stdout)
    assert data["catalog"]["task"] == "3d"
    assert data["reported"] == {"stage_specs": {"shape": {"controls": ["seed"]}}}
    assert fake.asked == ["m/Trellis"]


def test_render_nests_and_flattens():
    assert render({"a": {"b": [1, 2]}, "c": [], "d": {}}) == [
        "a:", "  b: 1, 2", "c: (none)", "d:", "  (none)"]
