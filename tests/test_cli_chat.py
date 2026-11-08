"""`tai ask` and `tai chat` end to end through click, against a fake device."""

import json

import pytest
from click.testing import CliRunner

import tai.cli as cli_mod
from tai.config import Settings

MODELS = [
    {"id": "q/Vision", "display_name": "Vision", "type": "Image-Text-to-Text", "npu_usage": 40,
     "status": "running", "input": "Text,Image", "thinking": {"toggleable": True}},
    {"id": "q/Coder", "display_name": "Coder", "type": "Text Generation", "npu_usage": 50,
     "status": "running", "input": "Text"},
]


class FakeClient:
    def __init__(self, settings=None):
        self.requests: list[dict] = []

    def models(self):
        return MODELS

    def running(self):
        return {"instances": {"running": []}}

    def _record(self, model, messages, extra):
        self.requests.append({"model": model, "messages": messages, **extra})

    def chat_stream(self, model, messages, **extra):
        self._record(model, messages, extra)
        yield {"choices": [{"delta": {"reasoning_content": "hmm"}}]}
        yield {"choices": [{"delta": {"content": f"answer {len(self.requests)}"}}]}

    def chat(self, model, messages, **extra):
        self._record(model, messages, extra)
        return {"choices": [{"message": {"content": "raw"}}]}


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeClient()
    monkeypatch.setattr(cli_mod, "TiinyClient", lambda settings: client)
    monkeypatch.setattr(cli_mod, "load_settings", lambda device=None: Settings("dev", "tok"))
    monkeypatch.setenv("TAI_HOME", str(tmp_path / "home"))
    return client


def run(*args, input=None):
    result = CliRunner().invoke(cli_mod.cli, list(args), input=input, catch_exceptions=False)
    return result


def test_ask_streams_answer_with_system_and_think(fake):
    r = run("ask", "-m", "vision", "-s", "be brief", "--no-think", "hello", "there")
    assert r.exit_code == 0 and "answer 1" in r.output and "hmm" not in r.output
    req = fake.requests[0]
    assert req["model"] == "q/Vision"
    assert req["messages"] == [{"role": "system", "content": "be brief"},
                               {"role": "user", "content": "hello there"}]
    assert req["chat_template_kwargs"] == {"enable_thinking": False}


def test_ask_think_flag_ignored_for_non_toggleable_model(fake):
    run("ask", "-m", "coder", "--think", "hi")
    assert "chat_template_kwargs" not in fake.requests[0]


def test_ask_reads_stdin_and_inlines_text_files(fake, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("file body")
    run("ask", "-m", "coder", "-f", str(f), "review:", input="piped body")
    content = fake.requests[0]["messages"][-1]["content"]
    assert content.startswith("review:\n\npiped body") and "--- notes.txt ---\nfile body" in content


def test_ask_image_needs_vision_model(fake, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    r = CliRunner().invoke(cli_mod.cli, ["ask", "-m", "coder", "-f", str(img), "what"])
    assert r.exit_code == 2 and "can't see images" in r.output
    run("ask", "-m", "vision", "-f", str(img), "what")
    assert fake.requests[0]["messages"][-1]["content"][1]["type"] == "image_url"


def test_ask_json_prints_raw_response(fake):
    r = run("ask", "-m", "coder", "--json", "hi")
    assert json.loads(r.output)["choices"][0]["message"]["content"] == "raw"


def test_ask_show_thinking_goes_to_stderr(fake):
    r = CliRunner().invoke(cli_mod.cli, ["ask", "-m", "vision", "--show-thinking", "hi"])
    assert "hmm" in r.stderr and "hmm" not in r.stdout


def test_chat_shows_thinking_unless_turned_off(fake):
    r = CliRunner().invoke(cli_mod.cli, ["chat", "-m", "vision"], input="hi\n")
    assert "hmm" in r.stderr
    r = CliRunner().invoke(cli_mod.cli, ["chat", "-m", "vision"], input="/think hide\nhi\n")
    assert "hmm" not in r.stderr and "answer" in r.stdout
    run("config", "set", "show_thinking", "off")
    r = CliRunner().invoke(cli_mod.cli, ["chat", "-m", "vision"], input="hi\n")
    assert "hmm" not in r.stderr


def test_chat_no_save_leaves_no_session(fake):
    run("config", "set", "think", "off")
    r = CliRunner().invoke(cli_mod.cli, ["chat", "-m", "vision", "--no-save"], input="hi\n")
    assert r.exit_code == 0 and "Not saved" in r.stderr
    assert fake.requests[0]["chat_template_kwargs"] == {"enable_thinking": False}  # settings still apply
    assert "hi" not in run("history").output
    r = run("chat", "-c", "--no-save", input="")
    assert r.exit_code == 2 and "can't resume" in r.output


def test_history_clear_asks_then_deletes_everything(fake):
    run("chat", "-m", "coder", input="first\n")
    r = run("history", "clear", input="n\n")
    assert r.exit_code == 1 and "first" in run("history").output
    r = run("history", "clear", "--yes")
    assert "Deleted 1 session " in r.output and "first" not in run("history").output


def test_ask_nothing_to_ask(fake):
    r = CliRunner().invoke(cli_mod.cli, ["ask"], input="")
    assert r.exit_code == 2 and "Nothing to ask" in r.output


def test_chat_new_session_then_continue(fake):
    r = run("chat", "-m", "coder", "-s", "sys", "-n", "work", input="first\n")
    assert r.exit_code == 0 and "answer 1" in r.output
    r = run("chat", "-c", "--no-think", input="second\n")
    assert "Resumed session 1 work (2 messages)" in r.output
    req = fake.requests[1]
    assert req["model"] == "q/Coder"
    assert [m["content"] for m in req["messages"]] == ["sys", "first", "answer 1", "second"]


def test_chat_resume_by_name_with_model_override(fake):
    run("chat", "-m", "coder", "-n", "work", input="first\n")
    run("chat", "-r", "work", "-m", "vision", "--no-think", input="second\n")
    assert fake.requests[1]["model"] == "q/Vision"
    assert fake.requests[1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_unknown_session(fake):
    r = CliRunner().invoke(cli_mod.cli, ["chat", "-r", "nope"], input="")
    assert r.exit_code == 2 and "No such session" in r.output
