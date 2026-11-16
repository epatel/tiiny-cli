"""`tai download`: list the store, or download a model with progress."""

import pytest
from click.testing import CliRunner

import tai.cli as cli_mod
from tai.config import Settings

INSTALLED = [{"id": "k2/Voice", "display_name": "Voice", "type": "Text-to-Speech", "npu_usage": 2, "status": "downloaded"}]
STORE = [{"name": "Voice", "fullname": "k2/Voice", "type": "Text-to-Speech"},
         {"name": "Kokoro-82M", "fullname": "hexgrad/Kokoro-82M", "type": "Text-to-Speech"},
         {"name": "Qwen3-VoiceClone-1.7B", "fullname": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "type": "Text-to-Speech"}]


class FakeClient:
    def __init__(self):
        self.downloads: list[str] = []

    def models(self):
        return INSTALLED

    def running(self):
        return {"instances": {"running": []}}

    def store_models(self):
        return STORE

    def download(self, model_id):
        self.downloads.append(model_id)
        yield {"_event": "progress", "progress": 50, "stage": "downloading", "downloaded_bytes": 10**8,
               "total_bytes": 2 * 10**8, "speed_bytes_per_sec": 1, "speed_human": "12 MB/s"}
        yield {"_event": "done", "progress": 100, "status": "downloaded"}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeClient()
    monkeypatch.setattr(cli_mod, "TiinyClient", lambda settings: client)
    monkeypatch.setattr(cli_mod, "load_settings", lambda **kw: Settings("dev", "tok"))
    monkeypatch.setenv("TAI_HOME", str(tmp_path))
    return client


def run(*args):
    return CliRunner().invoke(cli_mod.cli, list(args))


def test_without_a_model_it_lists_what_is_not_on_the_device(fake):
    r = run("download")
    assert r.exit_code == 0 and "Kokoro-82M" in r.output and "k2/Voice" not in r.output


def test_downloads_a_store_model_by_its_store_name(fake):
    r = run("download", "voiceclone")
    assert r.exit_code == 0, r.output
    assert fake.downloads == ["Qwen/Qwen3-TTS-12Hz-1.7B-Base"] and "Downloaded Qwen3-VoiceClone-1.7B" in r.output


def test_a_model_already_on_the_device_is_not_downloaded_again(fake):
    r = run("download", "k2/Voice")
    assert "already on the device" in r.output and fake.downloads == []


def test_an_unknown_model_lists_the_store(fake):
    r = run("download", "nothing-like-this")
    assert r.exit_code == 1 and "In the store:" in r.output
