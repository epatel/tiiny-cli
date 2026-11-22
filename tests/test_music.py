"""tai music: loops and songs, the SongGeneration preview/resume path, a dropped answer."""

import base64

import httpx
import pytest
from click.testing import CliRunner

import tai.cli as cli_mod
from tai.client import TiinyError
from tai.config import Settings
from tai.music import MusicJob, MusicOptions

WAV = base64.b64encode(b"RIFFsong").decode()
MODELS = [
    {"id": "m-a-p/YuE2-3B", "display_name": "YuE2-3B", "type": "Music Generation", "npu_usage": 40,
     "status": "running"},
    {"id": "RoyalCities/Foundation-1", "display_name": "Foundation-1", "type": "Music Generation",
     "npu_usage": 10, "status": "running"},
    {"id": "tencent/SongGeneration-v2-large", "display_name": "SongGeneration-v2-large",
     "type": "Music Generation", "npu_usage": 40, "status": "running"},
]


class FakeClient:
    def __init__(self, song=None, sessions=()):
        self.calls: list[tuple] = []
        self.answer = song or {"success": True, "audio_data": WAV, "session_id": "s1"}
        self.sessions = list(sessions)

    def models(self):
        return MODELS

    def running(self):
        return {"instances": {"running": []}}

    def music_info(self, model):
        return {"service_name": "SongGeneration-v2"} if "SongGeneration" in model else {}

    def music_loop(self, model, prompt, config):
        self.calls.append(("loop", prompt, config))
        return b"RIFFloop"

    def music_song(self, model, config, route="", session_id=None):
        self.calls.append(("song", route, config, session_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def music_sessions(self):
        return self.sessions.pop(0) if self.sessions else []

    def music_download(self, session_id, fmt="mp3"):
        self.calls.append(("download", session_id))
        return b"ID3song"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeClient()
    monkeypatch.setattr(cli_mod, "TiinyClient", lambda settings: client)
    monkeypatch.setattr(cli_mod, "load_settings", lambda **kw: Settings("dev", "tok"))
    monkeypatch.setattr(cli_mod.App, "ensure_loaded", lambda app, m: None)
    monkeypatch.setenv("TAI_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lyrics.txt").write_text("[verse]\nCity lights\n")
    return client


def run(*args, input=None):
    return CliRunner().invoke(cli_mod.cli, list(args), input=input, catch_exceptions=False)


def test_song_sends_caption_lyrics_and_saves_the_wav(fake, tmp_path):
    r = run("music", "-m", "yue", "-l", "lyrics.txt", "--seconds", "30", "--seed", "7", "synth", "pop")
    assert r.exit_code == 0, r.output
    (_, route, config, _), = fake.calls
    assert route == ""
    assert config == {"caption": "synth pop", "lyrics": "[verse]\nCity lights", "generation_type": "mixed",
                      "seconds_total": 30, "seed": 7}
    assert (tmp_path / "music.wav").read_bytes() == b"RIFFsong"


def test_lyrics_from_stdin_and_mp3_from_the_session(fake, tmp_path):
    r = run("music", "-m", "yue", "-l", "-", "-o", "song.mp3", "rock", input="la la\n")
    assert r.exit_code == 0, r.output
    assert fake.calls[0][2]["lyrics"] == "la la"
    assert fake.calls[1] == ("download", "s1")
    assert (tmp_path / "song.mp3").read_bytes() == b"ID3song"


def test_loop_model_gets_the_prompt_and_rejects_lyrics(fake, tmp_path):
    r = run("music", "-m", "foundation", "--seconds", "8", "--seed", "1", "warm rhodes, 90 bpm")
    assert r.exit_code == 0, r.output
    assert fake.calls == [("loop", "warm rhodes, 90 bpm", {"seconds_total": 8, "seed": 1})]
    assert run("music", "-m", "foundation", "-l", "lyrics.txt", "x").exit_code == 2


@pytest.mark.parametrize("args, message", [
    (["-m", "yue", "pop"], "needs --lyrics"),
    (["-m", "yue", "--instrumental", "pop"], "YuE2 only makes the full mix"),
])
def test_refuses_what_the_model_cant_do(fake, args, message):
    r = CliRunner().invoke(cli_mod.cli, ["music", *args])
    assert r.exit_code == 2 and message in r.output
    assert fake.calls == []


def test_instrumental_needs_no_lyrics(fake):
    assert run("music", "-m", "songgeneration", "--instrumental", "--seconds", "45", "ambient").exit_code == 0
    (_, route, config, _), = fake.calls
    assert route == "/preview"
    assert config["generation_type"] == "bgm" and config["preview_seconds"] == 45
    assert "seconds_total" not in config


def test_songgeneration_full_length_is_an_opening_then_resume():
    client = FakeClient()
    MusicJob(client).song("tencent/SongGeneration-v2-large", {"caption": "c", "seconds_total": 240}, staged=True)
    assert [(c[1], c[2].get("preview_seconds"), c[3]) for c in client.calls] == [
        ("/preview", 8, None), ("/resume", None, "s1")]


def test_a_dropped_answer_is_picked_up_from_the_sessions():
    sessions = [[], [{"session_id": "old", "model_id": "m-a-p/YuE2-3B", "status": "completed", "created_at": 1},
                     {"session_id": "new", "model_id": "m-a-p/YuE2-3B", "status": "completed", "created_at": 100}]]
    client = FakeClient(song=httpx.ReadError("dropped"), sessions=sessions)
    notes = []
    song = MusicJob(client, notes.append, sleep=lambda s: None, clock=lambda: 100.0).song(
        "m-a-p/YuE2-3B", {}, staged=False)
    assert (song.format, song.session, song.audio) == ("mp3", "new", b"ID3song")
    assert notes


def test_a_failed_session_is_reported():
    sessions = [[{"session_id": "x", "model_id": "m", "status": "failed", "created_at": 50, "error": "OOM"}]]
    client = FakeClient(song=httpx.ReadError("dropped"), sessions=sessions)
    with pytest.raises(TiinyError, match="OOM"):
        MusicJob(client, sleep=lambda s: None, clock=lambda: 50.0).song("m", {}, staged=False)


def test_style_reference_is_sent_as_base64(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"abc")
    config = MusicOptions(lyrics="x", style=ref, extra={"ace_step": {"bpm": 120}}).song_config("c")
    assert config["reference_audio_base64"] == "YWJj" and config["ace_step"] == {"bpm": 120}
