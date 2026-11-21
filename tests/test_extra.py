"""-x KEY=VALUE: extra request fields, parsed and placed per command."""

import click
import pytest
from click.testing import CliRunner

import tai.cli as cli_mod
from tai.config import Settings
from tai.options import AsrOptions, ImageOptions, TtsConfig, merge, parse_extras

MODELS = [
    {"id": "q/Coder", "display_name": "Coder", "type": "Text Generation", "npu_usage": 50,
     "status": "running", "input": "Text", "thinking": {"toggleable": True}},
    {"id": "k2/Voice", "display_name": "Voice", "type": "Text-to-Speech", "npu_usage": 10,
     "status": "running"},
    {"id": "k2-fsa/OmniVoice", "display_name": "OmniVoice", "type": "Text-to-Speech", "npu_usage": 2,
     "status": "running", "supported": ["Voice Clone", "Voice Design"]},
    {"id": "Qwen/Clone", "display_name": "Clone", "type": "Text-to-Speech", "npu_usage": 5,
     "status": "downloaded", "supported": ["Voice Clone"]},
    {"id": "z/Image", "display_name": "Image", "type": "Text-to-Image", "npu_usage": 20,
     "status": "running"},
    {"id": "q/Edit", "display_name": "Edit", "type": "Image-to-Image", "npu_usage": 30, "status": "running"},
    {"id": "q/Ears", "display_name": "Ears", "type": "ASR", "npu_usage": 5, "status": "running"},
    {"id": "q/Embed", "display_name": "Embed", "type": "Text Embedding", "npu_usage": 1,
     "status": "running"},
]


class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def models(self):
        return MODELS

    def running(self):
        return {"instances": {"running": []}}

    def chat_stream(self, model, messages, **extra):
        self.calls.append(("chat", extra))
        yield {"choices": [{"delta": {"content": "ok"}}]}

    def tts(self, model, text, config=None):
        self.calls.append(("tts", config))
        self.model = model
        return b"RIFF", {"duration": 1.0}

    def image(self, model, prompt, **extra):
        self.calls.append(("image", extra))
        return b"PNG"

    def asr(self, model, path, fields=None):
        self.calls.append(("asr", fields))
        return {"text": "hej"}

    def embed(self, model, inputs, **extra):
        self.calls.append(("embed", extra))
        return {"data": [{"index": 0, "embedding": [0.1]}]}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeClient()
    monkeypatch.setattr(cli_mod, "TiinyClient", lambda settings: client)
    monkeypatch.setattr(cli_mod, "load_settings", lambda **kw: Settings("dev", "tok"))
    monkeypatch.setenv("TAI_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return client


def run(*args):
    return CliRunner().invoke(cli_mod.cli, list(args), catch_exceptions=False)


def test_parse_values_and_nesting():
    got = parse_extras(None, None, ("language=sv", "temperature=0.2", "stop=[\"\\n\"]",
                                    "a.b=true", "a.c=x", "empty="))
    assert got == {"language": "sv", "temperature": 0.2, "stop": ["\n"],
                   "a": {"b": True, "c": "x"}, "empty": ""}


@pytest.mark.parametrize("bad", ["language", "=sv", "a=1 a.b=2"])
def test_parse_rejects_malformed(bad):
    with pytest.raises(click.BadParameter):
        parse_extras(None, None, tuple(bad.split(" ")))


def test_merge_is_deep_and_extra_wins():
    assert merge({"a": {"x": 1}, "b": 1}, {"a": {"y": 2}, "b": 2}) == {"a": {"x": 1, "y": 2}, "b": 2}


def test_option_bundles():
    assert TtsConfig(None, {"language": "sv"}).config("whisper") == {"instruct": "whisper", "language": "sv"}
    assert TtsConfig().config() is None
    assert AsrOptions("sv", {"prompt": "names", "n": 2}).fields() == {"language": "sv", "prompt": "names", "n": "2"}
    assert ImageOptions(extra={"seed": 5, "shift": 3}).request(1)["seed"] == 5  # -x overrides


def test_tts_puts_extras_in_config(fake):
    r = run("tts", "-i", "female", "-x", "language=sv", "hej")
    assert r.exit_code == 0
    assert fake.calls == [("tts", {"instruct": "female", "language": "sv"})]


def test_ask_merges_extras_with_thinking(fake):
    run("ask", "--no-think", "-x", "temperature=0.2", "-x", "chat_template_kwargs.foo=1", "hi")
    assert fake.calls == [("chat", {"temperature": 0.2,
                                    "chat_template_kwargs": {"enable_thinking": False, "foo": 1}})]


def test_image_asr_embed_take_extras(fake, tmp_path):
    (tmp_path / "a.wav").write_bytes(b"RIFF")
    run("image", "-x", "shift=3", "--seed", "7", "cat")
    run("asr", "-l", "sv", "-x", "prompt=Tiiny", "a.wav")
    run("embed", "-x", "dimensions=256", "hello")
    assert fake.calls[0] == ("image", {"size": "1024x1024", "seed": 7, "shift": 3})
    assert fake.calls[1] == ("asr", {"language": "sv", "prompt": "Tiiny"})
    assert fake.calls[2] == ("embed", {"dimensions": 256})


def test_bad_extra_is_a_usage_error(fake):
    r = CliRunner().invoke(cli_mod.cli, ["tts", "-x", "language", "hej"])
    assert r.exit_code == 2 and "KEY=VALUE" in r.output


def test_tts_clone_sends_the_recording_and_picks_a_model_that_clones(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod.App, "ensure_loaded", lambda app, m: None)
    (tmp_path / "me.wav").write_bytes(b"RIFF")
    r = run("tts", "-i", "female", "--clone", "me.wav", "--clone-text", "hello there", "hej")
    assert r.exit_code == 0, r.output
    assert fake.calls == [("tts", {"ref_audio": "UklGRg==", "ref_text": "hello there"})]  # no instruct
    assert fake.model == "Qwen/Clone"  # not the loaded OmniVoice: the device refuses its clone


def test_image_edits_pictures_given_with_i(fake, tmp_path):
    (tmp_path / "a.png").write_bytes(b"A")
    (tmp_path / "b.jpg").write_bytes(b"B")
    r = run("image", "-m", "Edit", "-i", "a.png", "--seed", "1", "make it winter")
    assert r.exit_code == 0 and "may ignore" not in r.output
    assert fake.calls[-1][1]["image"] == "data:image/png;base64,QQ==" and "images" not in fake.calls[-1][1]
    run("image", "-m", "Edit", "-i", "a.png", "-i", "b.jpg", "both")
    assert fake.calls[-1][1]["images"] == ["data:image/png;base64,QQ==", "data:image/jpeg;base64,Qg=="]


def test_image_warns_when_the_model_takes_no_pictures_and_caps_at_ten(fake, tmp_path):
    (tmp_path / "a.png").write_bytes(b"A")
    r = run("image", "-m", "z/Image", "-i", "a.png", "x")
    assert r.exit_code == 0 and "may ignore them" in r.output and "image" in fake.calls[-1][1]
    r = CliRunner().invoke(cli_mod.cli, ["image", *["-i", "a.png"] * 11, "x"])
    assert r.exit_code == 2 and "at most 10" in r.output
