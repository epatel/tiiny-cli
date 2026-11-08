"""The viewer server for real (random port) with an aiohttp client acting as the browser."""

import asyncio
import threading

import aiohttp
import pytest

from tai.agent.viewer import AskCancelled, Viewer, kind_for


@pytest.fixture
def viewer():
    opened = []
    v = Viewer(open_browser=opened.append)
    v.opened = opened
    v.start()
    yield v
    v.close()


def run(coro):
    return asyncio.run(coro)


async def connect(session, v):
    return await session.ws_connect(v.url + "ws", headers={"Origin": v.origin})


async def receive(ws, kind):
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), 5)
        if msg["type"] == kind:
            return msg


def test_kinds():
    from pathlib import Path
    assert [kind_for(Path(f"x.{e}")) for e in ("PNG", "wav", "glb", "html", "md", "zip")] == [
        "image", "audio", "model3d", "html", "markdown", "file"]


def test_page_needs_the_token_and_socket_the_origin(viewer):
    async def main():
        async with aiohttp.ClientSession() as s:
            async with s.get(viewer.url) as r:
                assert r.status == 200 and "tai viewer" in await r.text()
            async with s.get(f"{viewer.origin}/wrong-token/") as r:
                assert r.status == 404
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await s.ws_connect(viewer.url + "ws", headers={"Origin": "https://evil.example"})
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await s.ws_connect(viewer.url + "ws")  # no Origin (e.g. a sandboxed iframe sends "null")
    run(main())


def test_items_are_pushed_live_and_replayed_on_connect(viewer, tmp_path):
    img = tmp_path / "cat.png"
    img.write_bytes(b"\x89PNG data")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")

    async def main():
        async with aiohttp.ClientSession() as s:
            ws = await connect(s, viewer)
            assert (await receive(ws, "hello"))["items"] == []
            await asyncio.to_thread(viewer.present, "image", title="a cat", path=img)
            item = (await receive(ws, "item"))["item"]
            assert item == {"id": "1", "kind": "image", "title": "a cat", "url": "file/1/cat.png", "name": "cat.png"}
            async with s.get(viewer.url + item["url"]) as r:
                assert r.status == 200 and await r.read() == b"\x89PNG data"
            for bad in ("file/1/secret.txt", "file/9/cat.png", "file/1/../secret.txt"):
                async with s.get(viewer.url + bad) as r:
                    assert r.status == 404
            await asyncio.to_thread(viewer.present, "html", title="t", content="<b>hi</b>")
            await receive(ws, "item")
            ws2 = await connect(s, viewer)  # a reload sees the history
            assert [i["kind"] for i in (await receive(ws2, "hello"))["items"]] == ["image", "html"]
    run(main())


def test_ask_waits_for_the_browser_answer(viewer):
    result = {}

    def asker():
        result["answer"] = viewer.ask("Which one?", ["left", "right"])

    async def main():
        async with aiohttp.ClientSession() as s:
            ws = await connect(s, viewer)
            await receive(ws, "hello")
            t = threading.Thread(target=asker)
            t.start()
            item = (await receive(ws, "item"))["item"]
            assert item["kind"] == "ask" and item["choices"] == ["left", "right"] and item["answer"] is None
            await ws.send_json({"type": "answer", "id": item["id"], "value": "right"})
            assert (await receive(ws, "answered"))["answer"] == "right"
            await asyncio.to_thread(t.join, 5)
            await ws.send_json({"type": "answer", "id": item["id"], "value": "left"})  # second answer ignored
    run(main())
    assert result["answer"] == "right" and viewer.items[-1].answer == "right"


def test_ask_is_released_by_cancel_and_timeout(viewer):
    errors = []

    def asker(timeout):
        try:
            viewer.ask("Still there?", timeout=timeout)
        except AskCancelled as e:
            errors.append(str(e))

    t = threading.Thread(target=asker, args=(30,))
    t.start()
    while not viewer._answers:
        pass
    viewer.cancel_asks()
    t.join(5)
    asker(0.5)
    assert errors == ["interrupted", "no answer within 0s"]


def test_browser_opens_once_and_not_when_already_watching(viewer, tmp_path):
    viewer.present("text", content="one")
    viewer.present("text", content="two")
    assert viewer.opened == [viewer.url]
