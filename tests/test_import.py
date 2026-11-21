"""`tai import`: a Hugging Face model onto the device (inspect, then a streamed import)."""

import json

import httpx
import pytest
import respx
from click.testing import CliRunner

import tai.cli as cli_mod
from tai.client import TiinyClient, TiinyError
from tai.config import Settings
from tai.hfimport import HfModel, ImportChoice, ImportOptions, Inspection, event_state

URL = "https://huggingface.co/acme/Tiny-7B"
SUPPORTED = {"data": {
    "status": "supported",
    "model": {"fullname": "acme/Tiny-7B", "params": "7B", "size_bytes": 14_000_000_000, "hf_revision": "abc123"},
    "import_support": {"supported": True, "toolkit_id": "qwen-converter", "toolkit_revision": "3"},
    "hf": {"base_model_ids": ["Qwen/Qwen3-7B"]},
    "model_task": {"label": "Text Generation"},
}}
NEEDS_BASE = {"data": {
    "status": "action_required", "model": {"fullname": "acme/Lora"},
    "import_support": {"supported": False, "status": "action_required", "reason_code": "CONVERTER_BASE_MODEL_MISSING",
                       "required_base_model": {"model_id": "Qwen/Qwen3-7B", "display_name": "Qwen3-7B"}},
}}
UNSUPPORTED = {"data": {"model": {}, "import_support": {"supported": False, "reason_code": "MODEL_FILE_FORMAT_UNSUPPORTED"}}}


def test_model_given_as_org_name_or_url():
    assert HfModel.parse("acme/Tiny-7B") == HfModel(URL)
    assert HfModel.parse(URL + "/").name == "Tiny-7B"
    with pytest.raises(ValueError):
        HfModel.parse("Tiny-7B")


def test_request_is_filled_from_the_inspection_like_the_app():
    body = Inspection.of(SUPPORTED).request(ImportChoice(HfModel(URL), ImportOptions("Tiny", "a test", reasoning=True)))
    assert body == {"base_model_id": "Qwen/Qwen3-7B", "hf_url": URL, "hf_revision": "abc123",
                    "toolkit_id": "qwen-converter", "toolkit_revision": "3", "model_name": "Tiny",
                    "display_name": "Tiny", "model_desc": "a test", "reasoning_enabled": True}


def test_supported_missing_base_model_and_reasons():
    ok, base, no = Inspection.of(SUPPORTED), Inspection.of(NEEDS_BASE), Inspection.of(UNSUPPORTED)
    assert ok.supported and ok.needs_base_model is None and ok.size == 14_000_000_000 and ok.task == "Text Generation"
    assert not base.supported and base.needs_base_model == {"model_id": "Qwen/Qwen3-7B", "display_name": "Qwen3-7B"}
    assert not no.supported and ".safetensors" in no.why_not()


def test_stream_event_states():
    assert event_state("progress", {"status": "downloading", "progress": 40}) == "progress"
    assert event_state("progress", {"status": "failed"}) == "error"
    assert event_state("done", {"success": False}) == "error"
    assert event_state("done", {"success": True, "status": "completed", "stage_name": "complete", "progress": 100}) == "done"


def sse(*events):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


@respx.mock
def test_client_streams_an_import_until_done():
    route = respx.post("http://dev/api/v1/models/import").mock(return_value=httpx.Response(200, text=sse(
        ("progress", {"status": "downloading", "stage": "download", "progress": 30}),
        ("done", {"success": True, "status": "completed", "stage": "complete", "progress": 100}))))
    events = list(TiinyClient(Settings("dev", "tok")).import_model({"hf_url": URL}))
    assert [e["progress"] for e in events] == [30, 100]
    assert json.loads(route.calls[0].request.content) == {"hf_url": URL}


@respx.mock
def test_client_import_errors_are_in_words():
    respx.post("http://dev/api/v1/models/import").mock(return_value=httpx.Response(200, text=sse(
        ("error", {"status": "failed", "error_code": "HF_AUTH_FAILED", "reason_code": "HF_AUTH_FAILED"}))))
    with pytest.raises(TiinyError, match="must be public"):
        list(TiinyClient(Settings("dev", "tok")).import_model({"hf_url": URL}))
    respx.post("http://dev/api/v1/models/import/inspect").mock(return_value=httpx.Response(
        400, json={"detail": {"code": "INVALID_HF_URL", "message": "bad url"}}))
    with pytest.raises(TiinyError, match="Not a Hugging Face model URL"):
        TiinyClient(Settings("dev", "tok")).import_inspect("https://example.com/x")


class FakeClient:
    def __init__(self, inspection):
        self.inspection = inspection
        self.imported: list[dict] = []

    def models(self):
        return [{"id": "q/Coder", "display_name": "Coder", "type": "Text Generation", "npu_usage": 9, "status": "downloaded"}]

    def running(self):
        return {"instances": {"running": []}}

    def import_inspect(self, url):
        return self.inspection

    def import_model(self, request):
        self.imported.append(request)
        yield {"progress": 50, "stage": "convert"}

    def import_toolkits(self):
        return [{"id": "qwen", "display_name": "Qwen converter"}, {"id": "old", "compatibility": {"compatible": False}}]


def run(fake, monkeypatch, *args, input=None):
    monkeypatch.setattr(cli_mod, "TiinyClient", lambda settings: fake)
    monkeypatch.setattr(cli_mod, "load_settings", lambda **kw: Settings("dev", "tok"))
    return CliRunner().invoke(cli_mod.cli, list(args), input=input)


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("TAI_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")


def test_import_asks_then_streams(monkeypatch):
    fake = FakeClient(SUPPORTED)
    r = run(fake, monkeypatch, "import", "acme/Tiny-7B", "--name", "Tiny", input="y\n")
    assert r.exit_code == 0, r.output
    assert "7B" in r.output and "14.0 GB" in r.output and "Imported Tiny" in r.output
    assert fake.imported[0]["model_name"] == "Tiny" and fake.imported[0]["toolkit_id"] == "qwen-converter"


def test_check_only_and_refusals(monkeypatch):
    fake = FakeClient(SUPPORTED)
    assert run(fake, monkeypatch, "import", "acme/Tiny-7B", "--check").exit_code == 0 and fake.imported == []
    r = run(FakeClient(NEEDS_BASE), monkeypatch, "import", "acme/Lora")
    assert r.exit_code == 1 and "needs Qwen3-7B" in r.output and "tai download Qwen/Qwen3-7B" in r.output
    r = run(FakeClient(UNSUPPORTED), monkeypatch, "import", "acme/Bin")
    assert r.exit_code == 1 and ".safetensors" in r.output
    r = run(fake, monkeypatch, "import", "acme/Coder")
    assert r.exit_code == 1 and "already on the device" in r.output


def test_no_model_lists_usable_converters(monkeypatch):
    r = run(FakeClient(SUPPORTED), monkeypatch, "import")
    assert "Qwen converter" in r.output and "old" not in r.output and "1 converters" in r.output
