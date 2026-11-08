import io

import pytest
from rich.console import Console

from tai.chat import EXIT, ChatSession, SessionState
from tai.client import TiinyError
from tai.db import Store
from tai.models import Model, ResolveError


def model(name, vision=False):
    return Model(id=f"x/{name}", name=name, type="Text Generation", npu=1, status="running",
                 inputs=["Text", "Image"] if vision else ["Text"])


class FakeApp:
    def __init__(self, store, models):
        self.store, self.models, self.loaded = store, models, []

    def model_for(self, task, query):
        if query not in self.models:
            raise ResolveError(f"No model matches '{query}'")
        return self.models[query]

    def ensure_loaded(self, m):
        self.loaded.append(m.name)


@pytest.fixture
def session(tmp_path):
    store = Store(tmp_path / "t.db")
    plain, seer = model("plain"), model("seer", vision=True)
    app = FakeApp(store, {"plain": plain, "seer": seer})
    sid = store.create_session(plain.id)
    calls = []

    def reply(m, convo, think):
        calls.append((m.name, convo, think))
        return f"reply {len(calls)}"

    out = io.StringIO()
    s = ChatSession(app, SessionState(sid, plain), Console(file=out, width=200), reply)
    s.calls, s.out = calls, out
    return s


def test_send_records_history_and_persists(session):
    session.handle("hello")
    session.handle("again")
    convo = session.calls[1][1]
    assert [m["content"] for m in convo] == ["hello", "reply 1", "again"]
    rows = session.app.store.messages(session.state.sid)
    assert [r["content"] for r in rows] == ["hello", "reply 1", "again", "reply 2"]


def test_system_prompt_is_prepended_and_saved(session):
    session.handle("/system be brief")
    session.handle("hi")
    assert session.calls[0][1][0] == {"role": "system", "content": "be brief"}
    assert session.app.store.find_session(str(session.state.sid))["system"] == "be brief"


def test_think_toggle_is_passed_to_reply(session):
    session.handle("/think off")
    session.handle("hi")
    assert session.calls[0][2] is False


def test_clear_forgets_history(session):
    session.handle("hi")
    session.handle("/clear")
    session.handle("again")
    assert [m["content"] for m in session.calls[1][1]] == ["again"]
    assert len(session.app.store.messages(session.state.sid)) == 2


def test_model_switch_loads_and_reports_errors(session):
    session.handle("/model seer")
    assert session.state.model.name == "seer" and session.app.loaded == ["seer"]
    session.handle("/model nope")
    assert session.state.model.name == "seer" and "No model matches" in session.out.getvalue()


def test_image_requires_vision_and_is_sent_once(session, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG")
    session.handle(f"/image {img}")
    assert "can't see images" in session.out.getvalue() and not session.state.pending_images
    session.handle("/model seer")
    session.handle(f"/image {img}")
    session.handle("what is it")
    session.handle("and now")
    first, second = session.calls[0][1][-1], session.calls[1][1][-1]
    assert first["content"][1]["type"] == "image_url"
    assert second["content"] == "and now"


def test_failed_reply_keeps_history_clean(session):
    def boom(m, convo, think):
        raise TiinyError("device down")

    session.reply = boom
    session.handle("hi")
    assert session.state.messages == [] and "device down" in session.out.getvalue()


def test_exit_unknown_and_blank(session):
    assert session.handle("/exit") is EXIT
    assert session.handle("/q") is EXIT
    assert session.handle("   ") is None
    session.handle("/bogus")
    assert "unknown command /bogus" in session.out.getvalue()
