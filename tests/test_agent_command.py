import click
import pytest

from tai.agent.command import open_session
from tai.db import Store
from tai.models import Model
from tai.options import SessionRef

CODER = Model(id="q/Coder", name="Coder", type="Text Generation", npu=94, status="running",
              supported=["Tool Use"])
VISION = Model(id="q/Vision", name="Vision", type="Image-Text-to-Text", npu=47, status="running",
               supported=["Tool Use", "Reasoning"])


class FakeApp:
    def __init__(self, store):
        self.store = store

    def agent_model(self, query):
        return {"q/Coder": CODER, "vision": VISION, "q/Vision": VISION, None: CODER}[query]


@pytest.fixture
def app(tmp_path):
    return FakeApp(Store(tmp_path / "t.db"))


def test_new_session_is_tied_to_the_folder(app, tmp_path):
    s = open_session(app, SessionRef(), None, tmp_path)
    row = app.store.find_session(str(s.sid))
    assert (row["kind"], row["cwd"], s.model.name, s.messages) == ("agent", str(tmp_path), "Coder", [])


def test_continue_picks_latest_in_this_folder_with_full_messages(app, tmp_path):
    other = open_session(app, SessionRef(), None, tmp_path / "elsewhere")
    mine = open_session(app, SessionRef(), None, tmp_path)
    app.store.add_raw_message(mine.sid, {"role": "user", "content": "hi"})
    app.store.add_raw_message(other.sid, {"role": "user", "content": "other"})  # newer, other folder
    s = open_session(app, SessionRef(cont=True), None, tmp_path)
    assert s.sid == mine.sid and s.messages == [{"role": "user", "content": "hi"}]


def test_resume_by_id_with_model_override(app, tmp_path):
    first = open_session(app, SessionRef(), None, tmp_path)
    s = open_session(app, SessionRef(resume=str(first.sid)), "vision", tmp_path)
    assert s.sid == first.sid and s.model.name == "Vision"


def test_resume_errors(app, tmp_path):
    with pytest.raises(click.UsageError, match="in this folder"):
        open_session(app, SessionRef(cont=True), None, tmp_path)
    chat = app.store.create_session("q/Coder")
    with pytest.raises(click.UsageError, match="is a chat session"):
        open_session(app, SessionRef(resume=str(chat)), None, tmp_path)


def test_agent_thinking_has_its_own_setting(app):
    from tai.agent.command import AGENT_THINK_SETTINGS
    from tai.cli import _think_extra

    thinker = Model(id="q/Q38", name="Q38", type="Image-Text-to-Text", npu=52, status="running",
                    thinking={"toggleable": True})

    def agent(flag=None):
        return _think_extra(app, thinker, flag, settings=AGENT_THINK_SETTINGS)

    def chat():
        return _think_extra(app, thinker, None)

    off, on = ({"chat_template_kwargs": {"enable_thinking": v}} for v in (False, True))
    assert agent() == {} and chat() == {}            # nothing set: the model's default
    app.store.set_setting("think", "on")
    assert agent() == on and chat() == on            # agent falls back to `think`
    app.store.set_setting("agent.think", "off")
    assert agent() == off and chat() == on           # agent.think wins for the agent only
    assert agent(flag=True) == on                    # --think beats both
    assert _think_extra(app, CODER, True, settings=AGENT_THINK_SETTINGS) == {}  # can't toggle: nothing sent
