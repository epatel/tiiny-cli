"""`tai serve` end to end: a fake device and the server run in a background
thread; the real TiinyClient talks to the server in client mode."""

import asyncio
import json
import socket
import threading

import pytest
from aiohttp import web

from tai.client import TiinyClient, TiinyError
from tai.config import Settings
from tai.serve import ServeConfig, make_app

DEVICE_KEY, API_KEY = "device-secret", "client-key"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def sse(request: web.Request, events: list[str], delay: float = 0) -> web.StreamResponse:
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    for data in events:
        await resp.write(f"data: {data}\n\n".encode())
        await asyncio.sleep(delay)
    return resp


def fake_device(seen: list) -> web.Application:
    chat = [json.dumps({"choices": [{"delta": {"content": w}}]}) for w in ("Hel", "lo")] + ["[DONE]"]

    async def handler(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        seen.append({"method": request.method, "path": request.path, "query": request.query_string,
                     "auth": request.headers.get("Authorization"), "body": body})
        if request.path == "/v1/chat/completions":
            return await sse(request, chat)
        if request.path == "/v1/slow":  # a long stream the client abandons
            return await sse(request, [str(i) for i in range(20)], delay=0.05)
        if request.path == "/api/v1/models/":
            return web.json_response({"data": [{"id": "q/Coder"}]})
        return web.json_response({"error": {"message": "nope"}}, status=404)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


@pytest.fixture
def stack():
    """(server URL, requests the device saw, server log lines)."""
    seen, logs = [], []
    loop = asyncio.new_event_loop()
    dport, sport = free_port(), free_port()
    cfg = ServeConfig(Settings(f"127.0.0.1:{dport}", DEVICE_KEY), API_KEY)
    runners = []

    async def start():
        for app, port in ((fake_device(seen), dport), (make_app(cfg, logs.append), sport)):
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            runners.append(runner)

    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start())
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(5)
    yield f"http://127.0.0.1:{sport}", seen, logs

    async def stop():
        for r in runners:
            await r.cleanup()
    asyncio.run_coroutine_threadsafe(stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)


def client(url, key=API_KEY):
    return TiinyClient(Settings(url, key, via_server=True))


def test_client_mode_forwards_with_the_device_key_under_a_prefix(stack):
    url, seen, logs = stack
    c = client(url + "/tai/")  # as if nginx passed its /tai prefix through unstripped
    assert c.models() == [{"id": "q/Coder"}]
    assert seen[0]["path"] == "/api/v1/models/" and seen[0]["auth"] == f"Bearer {DEVICE_KEY}"
    assert any("/api/v1/models/ 200" in line for line in logs)


def test_streamed_chat_and_post_body_pass_through(stack):
    url, seen, _ = stack
    chunks = list(client(url).chat_stream("q/Coder", [{"role": "user", "content": "hi"}]))
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    assert text == "Hello"
    assert json.loads(seen[0]["body"])["messages"][0]["content"] == "hi"


def test_wrong_or_missing_key_is_refused_before_the_device(stack):
    url, seen, _ = stack
    with pytest.raises(TiinyError, match="401.*API key"):
        client(url, key="wrong").models()
    assert seen == []


def test_only_device_routes_are_forwarded_and_healthz_needs_no_key(stack):
    import httpx

    url, seen, _ = stack
    r = httpx.get(url + "/kb/sources", headers={"Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 404 and seen == []
    assert httpx.get(url + "/tai/healthz").json() == {"ok": True}


def test_query_strings_are_forwarded(stack):
    url, seen, _ = stack
    client(url).http.get("/api/v1/models/", params={"including_downloading": "true"})
    assert seen[0]["query"] == "including_downloading=true"


def test_a_client_hanging_up_mid_stream_is_not_a_device_error(stack):
    import time

    url, _, logs = stack
    c = client(url)
    with c.http.stream("GET", "/v1/slow") as r:
        next(r.iter_lines())  # read one event, then hang up
    for _ in range(100):
        if any("client disconnected" in line for line in logs):
            break
        time.sleep(0.02)
    assert any("/v1/slow client disconnected" in line for line in logs)
    assert not any("unreachable" in line for line in logs)


def test_device_path_ignores_whatever_prefix_nginx_leaves():
    from tai.serve import device_path

    assert device_path("/v1/models") == "/v1/models"
    assert device_path("/tai/v1/models") == "/v1/models"
    assert device_path("/a/b/api/v1/models/running") == "/api/v1/models/running"
    assert device_path("/tai/healthz") == device_path("/healthz") == "/healthz"
    assert device_path("/kb/sources") is None and device_path("/") is None


def test_device_errors_come_back_unchanged(stack):
    url, _, _ = stack
    with pytest.raises(TiinyError, match="404.*nope"):
        client(url).task3d("missing")
