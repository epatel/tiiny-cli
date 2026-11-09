"""`tai video`: options checked against the model's /v1/video/info, then submit → poll → MP4."""

from contextlib import contextmanager

import pytest
from click.testing import CliRunner

import tai.cli as cli_mod
from tai.client import TiinyError
from tai.config import Settings
from tai.video import VideoJob, VideoOptions

# as the device answers (TiinyOS 0.1.39, 2026-11-09)
INFO = {"canvases": [{"height": 768, "width": 1344}, {"height": 1344, "width": 768},
                     {"height": 480, "width": 832}, {"height": 832, "width": 480}],
        "step_options": [4, 5, 6, 7, 8], "default_steps": 7, "fps": 24}

MODELS = [{"id": "lx/MiniMax-H3-Turbo", "display_name": "MiniMax-H3-Turbo", "type": "Text-to-Video",
           "npu_usage": 49, "status": "running", "input": "Text,Image"}]


class FakeClient:
    def __init__(self, polls=None):
        self.submitted: list[dict] = []
        self.polls = list(polls or [{"status": "in_progress", "progress": 50}, {"status": "completed"}])

    def models(self):
        return MODELS

    def running(self):
        return {"instances": {"running": []}}

    def video_info(self, model):
        return INFO

    def video_submit(self, model, prompt, **fields):
        self.submitted.append({"model": model, "prompt": prompt, **fields})
        return "vid-1"

    def video(self, video_id):
        item = self.polls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def video_content(self, video_id):
        return b"MP4"


@contextmanager
def no_progress(label):
    yield lambda pct, msg: None


def test_request_fields(tmp_path):
    frame = tmp_path / "start.png"
    frame.write_bytes(b"\x89PNG")
    body = VideoOptions(seconds=2, steps=4, image=frame, extra={"steps": 5, "x": 1}).request(seed=9)
    assert body["seconds"] == "2" and body["seed"] == 9 and body["size"] == "832x480"
    assert body["image"].startswith("data:image/png;base64,")
    assert body["steps"] == 5 and body["x"] == 1  # -x wins


def test_problems_follow_the_models_info():
    assert VideoOptions().problems(INFO) == []
    assert VideoOptions(seconds=8).problems({})[0].startswith("--seconds must be 1-7")
    assert VideoOptions(size="1920x1080").problems(INFO) == [
        "--size must be one of 1344x768, 768x1344, 832x480, 480x832"]
    assert VideoOptions(size="1920x1080").problems({"canvases": ["1920x1080"]}) == []  # plain strings too
    assert VideoOptions(steps=12).problems(INFO) == ["--steps must be one of 4, 5, 6, 7, 8"]
    assert VideoOptions(size="1920x1080", steps=12).problems({}) == []  # model didn't say


def test_render_polls_and_retries_gateway_errors():
    client = FakeClient([TiinyError("HTTP 504 GET /v1/videos/vid-1: timeout"),
                         {"status": "queued"}, {"status": "completed"}])
    sleeps = []
    assert VideoJob(client, no_progress, sleeps.append).render("m", "a cat", {}) == b"MP4"
    assert client.polls == [] and len(sleeps) == 2


def test_render_reports_the_devices_reason():
    client = FakeClient([{"status": "failed", "error": {"code": "GENERATION_TIMEOUT",
                                                       "message": "video generation exceeded the gateway time limit"}}])
    with pytest.raises(TiinyError, match="video failed: video generation exceeded"):
        VideoJob(client, no_progress, lambda s: None).render("m", "a cat", {})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeClient()
    monkeypatch.setattr(cli_mod, "TiinyClient", lambda settings: client)
    monkeypatch.setattr(cli_mod, "load_settings", lambda **kw: Settings("dev", "tok"))
    monkeypatch.setattr(cli_mod.App, "ensure_loaded", lambda app, m: None)
    monkeypatch.setattr("tai.video.time.sleep", lambda s: None)
    monkeypatch.setenv("TAI_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return client


def run(*args):
    return CliRunner().invoke(cli_mod.cli, list(args))


def test_cli_renders_and_saves(fake, tmp_path):
    r = run("video", "--seconds", "2", "--seed", "3", "-o", "clip.mp4", "a", "red", "fox")
    assert r.exit_code == 0, r.output
    assert (tmp_path / "clip.mp4").read_bytes() == b"MP4" and "seed 3" in r.output
    assert fake.submitted == [{"model": "lx/MiniMax-H3-Turbo", "prompt": "a red fox",
                               "size": "832x480", "seconds": "2", "seed": 3}]


def test_cli_rejects_what_the_model_cant_do(fake):
    r = run("video", "--size", "1920x1080", "a fox")
    assert r.exit_code == 2 and "--size must be one of" in r.output and fake.submitted == []
    r = run("video", "--seconds", "10", "a fox")
    assert r.exit_code == 2 and "--seconds" in r.output


def test_info_asks_the_video_model(fake):
    r = run("info", "minimax")
    assert "step_options: 4, 5, 6, 7, 8" in r.output and "width: 1344" in r.output
