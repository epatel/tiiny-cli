import base64
import json

import httpx
import pytest
import respx

from tai.client import TiinyClient, TiinyError, iter_sse
from tai.config import Settings

BASE = "http://tiiny.test"


@pytest.fixture
def client():
    return TiinyClient(Settings(device="tiiny.test", token="tok"))


def test_iter_sse_events():
    lines = ["event: progress", 'data: {"p": 1}', "", ": comment", "data: a", "data: b", ""]
    assert list(iter_sse(iter(lines))) == [("progress", '{"p": 1}'), ("message", "a\nb")]


@respx.mock
def test_bearer_token_and_error_message(client):
    route = respx.get(f"{BASE}/api/v1/models/").mock(
        return_value=httpx.Response(404, json={"error": {"message": "nope", "type": "x"}}))
    with pytest.raises(TiinyError, match="HTTP 404 GET /api/v1/models/: nope"):
        client.models()
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"


@respx.mock
def test_launch_streams_progress(client):
    body = ('event: progress\ndata: {"progress": 50}\n\n'
            'event: done\ndata: {"progress": 100, "success": true}\n\n')
    respx.post(f"{BASE}/api/v1/models/Qwen/X/launch/stream").mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    evs = list(client.launch("Qwen/X"))
    assert [e["_event"] for e in evs] == ["progress", "done"] and evs[-1]["success"]


@respx.mock
def test_chat_stream_yields_chunks_until_done(client):
    chunks = [{"choices": [{"delta": {"content": "Hi"}}]}, {"choices": [{"delta": {"content": "!"}}]}]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    route = respx.post(f"{BASE}/v1/chat/completions").mock(return_value=httpx.Response(200, text=body))
    got = list(client.chat_stream("m", [{"role": "user", "content": "x"}], max_tokens=5))
    assert [c["choices"][0]["delta"]["content"] for c in got] == ["Hi", "!"]
    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True and sent["max_tokens"] == 5


@respx.mock
def test_tts_uses_native_synthesize_and_config(client):
    route = respx.post(f"{BASE}/v1/synthesize").mock(return_value=httpx.Response(
        200, json={"success": True, "audio_data": base64.b64encode(b"RIFF").decode(), "duration": 1.0}))
    audio, meta = client.tts("k2-fsa/OmniVoice", "hi", {"instruct": "whisper"})
    assert audio == b"RIFF" and meta["duration"] == 1.0
    assert json.loads(route.calls[0].request.content)["config"] == {"instruct": "whisper"}


@respx.mock
def test_image_decodes_b64(client):
    respx.post(f"{BASE}/v1/images/generations").mock(return_value=httpx.Response(
        200, json={"data": [{"b64_json": base64.b64encode(b"\x89PNG").decode()}]}))
    assert client.image("m", "cat") == b"\x89PNG"


@respx.mock
def test_task3d_submit_encodes_refs(client):
    route = respx.post(f"{BASE}/v1/3d/tasks").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {"task_id": "t1"}}))
    refs = {"shape_artifact": {"task_id": "a", "artifact_id": "b"}}
    assert client.task3d_submit("texture", input_refs=refs) == "t1"
    from urllib.parse import parse_qs  # no file -> urlencoded form; the device accepts both
    form = parse_qs(route.calls[0].request.content.decode())
    assert form["stage"] == ["texture"] and json.loads(form["input_refs"][0]) == refs


@respx.mock
def test_launch_idle_stream_times_out_with_clear_error(client):
    respx.post(f"{BASE}/api/v1/models/Qwen/X/launch/stream").mock(side_effect=httpx.ReadTimeout("idle"))
    with pytest.raises(TiinyError, match="No progress from the device for 180s while loading Qwen/X"):
        list(client.launch("Qwen/X"))
    assert respx.calls[0].request.extensions["timeout"]["read"] == 180.0
