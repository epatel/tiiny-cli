"""`tai download`, `tai update`, `tai rm`: the store, downloads with progress, updates, deletes."""

import pytest
from click.testing import CliRunner

import tai.cli as cli_mod
from tai.config import Settings

INSTALLED = [{"id": "k2/Voice", "display_name": "Voice", "type": "Text-to-Speech", "npu_usage": 2, "status": "downloaded",
              "version_label": "20261029", "need_update": True, "total_size": 1_700_000_000},
             {"id": "q/Coder", "display_name": "Coder", "type": "Text Generation", "npu_usage": 94, "status": "running",
              "version_label": "20260923", "need_update": True},
             {"id": "q/ASR", "display_name": "ASR", "type": "ASR", "npu_usage": 7, "status": "downloaded",
              "version_label": "20261111"}]
STORE = [{"name": "Voice", "fullname": "k2/Voice", "type": "Text-to-Speech", "need_update": True},
         {"name": "Kokoro-82M", "fullname": "hexgrad/Kokoro-82M", "type": "Text-to-Speech"},
         {"name": "Qwen3-VoiceClone-1.7B", "fullname": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "type": "Text-to-Speech"}]


class FakeClient:
    def __init__(self):
        self.installed = list(INSTALLED)
        self.log: list[tuple[str, str]] = []
        self.disk_ok = True
        self.settings = Settings("dev", "tok")

    @property
    def downloads(self):
        return [model_id for what, model_id in self.log if what == "download"]

    def models(self):
        return self.installed

    def running(self):
        return {"instances": {"running": []}}

    def store_models(self):
        return STORE

    def downloading(self):
        return [{"id": "hexgrad/Kokoro-82M", "status": "downloading", "progress": 37.4}]

    def npu(self):
        return {"npu_used": 94, "npu_total": 100, "npu_available": 6,
                "models": [{"model_id": "q/Coder", "npu_usage": 94, "status": "running"}]}

    def update_info(self, model_id):
        row = next(d for d in self.installed if d["id"] == model_id)
        return {"need_update": bool(row.get("need_update")), "local_version": row["version_label"],
                "cloud_version": "20261119", "disk_ok": self.disk_ok, "required_bytes": 527_000_000,
                "available_bytes": 100_000_000}

    def start_update(self, model_id):
        self.log.append(("update", model_id))

    def delete(self, model_id):
        self.log.append(("delete", model_id))
        self.installed = [d for d in self.installed if d["id"] != model_id]

    def download(self, model_id):
        self.log.append(("download", model_id))
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


def run(*args, input=None):
    return CliRunner().invoke(cli_mod.cli, list(args), input=input)


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
    assert "tai update Voice" in r.output  # the store says it has a newer version


def test_update_without_a_model_lists_installed_and_available_versions(fake):
    r = run("update")
    voice = next(line for line in r.output.splitlines() if "k2/Voice" in line)
    assert "20261029" in voice and "20261119" in voice
    assert "q/Coder" in r.output and "q/ASR" not in r.output and fake.log == []


def test_update_asks_then_replaces_the_model_and_follows_the_download(fake):
    r = run("update", "voice", input="n\n")
    assert "20261029 → 20261119" in r.output and fake.log == []
    r = run("-y", "update", "voice")
    assert r.exit_code == 0, r.output
    assert fake.log == [("update", "k2/Voice"), ("download", "k2/Voice")]
    assert "Updated Voice to 20261119" in r.output


def test_update_refuses_what_is_up_to_date_loaded_or_too_big(fake):
    assert "ASR is up to date (20261111)" in run("-y", "update", "asr").output
    r = run("-y", "update", "coder")
    assert r.exit_code == 1 and "tai unload Coder" in r.output
    fake.disk_ok = False
    r = run("-y", "update", "voice")
    assert r.exit_code == 1 and "Not enough disk space" in r.output and "0.5 GB" in r.output
    assert fake.log == []


def test_rm_asks_before_deleting_and_refuses_a_loaded_model(fake):
    r = run("rm", "voice", input="\n")  # Enter = no
    assert "Delete Voice (k2/Voice, 1.7 GB)" in r.output and fake.log == []
    r = run("-y", "rm", "voice")
    assert fake.log == [("delete", "k2/Voice")] and "Deleted Voice" in r.output
    assert "k2/Voice" not in run("models").output
    r = run("-y", "rm", "coder")
    assert r.exit_code == 1 and "tai unload Coder" in r.output and len(fake.log) == 1


def test_status_lists_a_download_in_progress(fake):
    r = run("status")
    assert "● q/Coder  94%" in r.output and "↓ hexgrad/Kokoro-82M  37%  downloading" in r.output


def test_an_unknown_model_lists_the_store(fake):
    r = run("download", "nothing-like-this")
    assert r.exit_code == 1 and "In the store:" in r.output
