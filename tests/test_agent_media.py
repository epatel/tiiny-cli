"""image / show / ask tools with a fake device and a fake viewer."""

from contextlib import contextmanager

import pytest

from tai.agent.media import MediaContext, free_path, media_tools, slug
from tai.agent.tools import ToolContext, ToolError
from tai.agent.viewer import AskCancelled


class FakeViewer:
    url = "http://127.0.0.1:1/tok/"

    def __init__(self, answer="left"):
        self.shown, self.asked, self.answer = [], [], answer

    def present(self, kind, *, title="", path=None, content=None):
        self.shown.append((kind, title, path.name if path else None, content))

    def ask(self, question, choices=None):
        self.asked.append((question, choices))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeDevice:
    """Coder (agent) loaded at 94%; the image model needs 23%, the video model 49%."""

    def __init__(self):
        self.status = {"q/Coder": "running", "t/Image": "downloaded", "lx/Video": "downloaded"}
        self.log = []
        self.polls = [{"status": "in_progress", "progress": 40}, {"status": "completed"}]
        self.on_poll = None

    def models(self):
        spec = {"q/Coder": ("Text Generation", 94), "t/Image": ("Text-to-Image", 23),
                "lx/Video": ("Text-to-Video", 49)}
        return [{"id": i, "display_name": i.split("/")[1], "type": t, "npu_usage": n, "status": self.status[i]}
                for i, (t, n) in spec.items()]

    def running(self):
        return {"instances": {"running": [{"model_id": i, "status": "running"}
                                          for i, st in self.status.items() if st == "running"]}}

    def npu(self):
        return {"npu_total": 100}

    def stop(self, model_id):
        self.log.append(("stop", model_id))
        self.status[model_id] = "downloaded"

    def launch(self, model_id):
        self.log.append(("launch", model_id))
        self.status[model_id] = "running"
        yield {"_event": "done", "progress": 100, "success": True}

    def image(self, model, prompt, size="1024x1024", **extra):
        self.log.append(("image", model, prompt, size))
        self.extra = extra
        return b"\x89PNG" + b"0" * 2048

    def video_info(self, model):
        return {"canvases": [{"width": 832, "height": 480}, {"width": 1344, "height": 768}],
                "step_options": [4, 5, 6, 7, 8]}

    def video_submit(self, model, prompt, **fields):
        self.log.append(("video", model, prompt))
        self.video_fields = fields
        return "v1"

    def video(self, video_id):
        if self.on_poll:
            self.on_poll()
        return self.polls.pop(0)

    def video_content(self, video_id):
        return b"MP4" + b"0" * 4096


class FakeUI:
    def __init__(self, *answers):
        self.answers, self.questions, self.infos = list(answers), [], []

    def confirm(self, question):
        self.questions.append(question)
        return self.answers.pop(0)

    def info(self, message):
        self.infos.append(message)

    @contextmanager
    def progress(self, name):
        yield lambda pct, msg: None

    @contextmanager
    def activity(self, label):
        self.infos.append(label)
        yield lambda pct, msg: None


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(root=tmp_path.resolve())


def tools_for(*answers, viewer=None):
    m = MediaContext(FakeDevice(), viewer or FakeViewer(), agent_model="q/Coder", ui=FakeUI(*answers))
    return media_tools(m), m


def test_slug_and_free_path(tmp_path):
    assert slug("Batman, in the RAIN!") == "batman-in-the-rain" and slug("!!!") == "image"
    (tmp_path / "a.png").write_bytes(b"")
    (tmp_path / "a-2.png").write_bytes(b"")
    assert free_path(tmp_path / "a.png").name == "a-3.png"


def test_image_asks_before_unloading_the_agent_then_saves_and_shows(ctx):
    tools, m = tools_for(True)
    out = tools["image"].run({"prompt": "Batman in the rain", "seed": 15, "guidance_scale": 3.5}, ctx)
    assert out == "Saved images/batman-in-the-rain.png (2 KB, seed 15) and showed it in the viewer."
    assert m.client.extra == {"seed": 15, "guidance_scale": 3.5}
    assert "Coder (94%) — the agent's model" in m.ui.questions[0]
    assert m.client.log == [("stop", "q/Coder"), ("launch", "t/Image"),
                            ("image", "t/Image", "Batman in the rain", "1024x1024")]
    assert (ctx.root / "images" / "batman-in-the-rain.png").read_bytes().startswith(b"\x89PNG")
    assert m.viewer.shown == [("image", "Batman in the rain", "batman-in-the-rain.png", None)]


def test_image_never_overwrites_and_stays_inside_the_project(ctx, tmp_path_factory):
    tools, m = tools_for(True)
    (ctx.root / "logo.png").write_bytes(b"keep me")
    out = tools["image"].run({"prompt": "logo", "path": "logo.png", "size": "512x512"}, ctx)
    assert "Saved logo-2.png" in out
    assert f"seed {m.client.extra['seed']})" in out  # no seed given: a random one, reported back
    assert (ctx.root / "logo.png").read_bytes() == b"keep me"
    outside = tmp_path_factory.mktemp("elsewhere") / "x.png"
    with pytest.raises(ToolError, match="inside the project"):
        tools["image"].run({"prompt": "x", "path": str(outside)}, ctx)


def test_image_declined_swap_generates_nothing(ctx):
    tools, m = tools_for(False)
    with pytest.raises(ToolError, match="declined"):
        tools["image"].run({"prompt": "x"}, ctx)
    assert m.client.log == [] and m.viewer.shown == []


def test_image_needs_the_ui(ctx):
    tools, m = tools_for()
    m.ui = None
    with pytest.raises(ToolError, match="interactive UI"):
        tools["image"].run({"prompt": "x"}, ctx)


def test_show_files_and_inline_content(ctx):
    tools, m = tools_for()
    (ctx.root / "robot.glb").write_bytes(b"glTF")
    tools["show"].run({"path": "robot.glb"}, ctx)
    tools["show"].run({"markdown": "# Hi", "title": "notes"}, ctx)
    assert m.viewer.shown == [("model3d", "robot.glb", "robot.glb", None), ("markdown", "notes", None, "# Hi")]
    with pytest.raises(ToolError, match="File not found"):
        tools["show"].run({"path": "missing.png"}, ctx)
    with pytest.raises(ToolError, match="Give a path"):
        tools["show"].run({"title": "nothing"}, ctx)


def test_ask_returns_the_answer_or_reports_no_answer(ctx):
    tools, m = tools_for()
    assert tools["ask"].run({"question": "Which?", "choices": ["left", "right"]}, ctx) == "The user answered: left"
    assert m.viewer.asked == [("Which?", ["left", "right"])]
    tools, m = tools_for(viewer=FakeViewer(answer=AskCancelled("interrupted")))
    with pytest.raises(ToolError, match=r"No answer from the user \(interrupted\)"):
        tools["ask"].run({"question": "?"}, ctx)


def test_tools_never_need_the_write_permission():
    tools, _ = tools_for()
    assert {name: t.grant for name, t in tools.items()} == {"image": None, "video": None, "show": None, "ask": None}
    assert tools["image"].summary({"prompt": "a cat"}) == "image a cat"


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr("tai.agent.media.threading.Event.wait", lambda self, t=None: self.is_set())


def test_video_asks_before_unloading_the_agent_then_saves_and_shows(ctx, no_wait):
    (ctx.root / "fox.png").write_bytes(b"\x89PNG")
    tools, m = tools_for(True)
    out = tools["video"].run({"prompt": "A fox runs", "seconds": 2, "steps": 4, "seed": 7,
                              "image": "fox.png"}, ctx)
    assert out == "Saved videos/a-fox-runs.mp4 (4 KB, seed 7) and showed it in the viewer."
    assert "the agent's model" in m.ui.questions[0]  # coder 94% + video 49% don't fit
    assert ("stop", "q/Coder") in m.client.log and ("video", "lx/Video", "A fox runs") in m.client.log
    fields = m.client.video_fields
    assert fields["seconds"] == "2" and fields["steps"] == 4 and fields["seed"] == 7
    assert fields["image"].startswith("data:image/png;base64,")
    assert m.viewer.shown == [("video", "A fox runs", "a-fox-runs.mp4", None)]
    assert "Rendering video (2s, 832x480)" in m.ui.infos


def test_video_checks_options_before_loading_and_against_the_model(ctx, no_wait):
    tools, m = tools_for(True)
    with pytest.raises(ToolError, match="seconds must be 1-7"):
        tools["video"].run({"prompt": "x", "seconds": 9}, ctx)
    assert m.client.log == [] and m.ui.questions == []  # nothing loaded
    with pytest.raises(ToolError, match="size must be one of 832x480, 1344x768"):
        tools["video"].run({"prompt": "x", "size": "1920x1080"}, ctx)
    with pytest.raises(ToolError, match="Start image not found"):
        tools["video"].run({"prompt": "x", "image": "missing.png"}, ctx)


def test_esc_stops_waiting_for_a_render(ctx, no_wait):
    tools, m = tools_for(True)
    m.client.on_poll = m.cancel.set  # the user presses Esc while it renders
    with pytest.raises(ToolError, match="Interrupted. The device keeps rendering"):
        tools["video"].run({"prompt": "x"}, ctx)
    assert not (ctx.root / "videos").exists()
