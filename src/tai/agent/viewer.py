"""Browser viewer for `tai agent`: a live feed of what the agent shows (images, audio,
3D models, HTML/Markdown, files) and questions it asks, over HTTP + WebSockets.

Safety: binds 127.0.0.1 on a random port; every URL carries a random per-session
token; the WebSocket only accepts the viewer's own origin (so other web pages can't
connect); only files the agent explicitly presented are served; agent HTML and
Markdown render in a sandboxed iframe without access to the page or the socket.

Runs on its own asyncio loop in a background thread (like the MCP hub). ``present``
and ``ask`` are called from the agent's worker thread.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import WSMsgType, web

PAGE = Path(__file__).with_name("viewer.html")
ASK_TIMEOUT = 600.0

KINDS = {  # file suffix -> how the page shows it
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"), "image"),
    **dict.fromkeys((".wav", ".mp3", ".ogg", ".flac", ".m4a"), "audio"),
    **dict.fromkeys((".mp4", ".webm", ".mov"), "video"),
    **dict.fromkeys((".glb", ".gltf"), "model3d"),
    ".html": "html", ".htm": "html", ".md": "markdown",
}


class AskCancelled(Exception):
    pass


@dataclass
class Item:
    id: str
    kind: str  # image | audio | video | model3d | html | markdown | text | file | ask
    title: str = ""
    path: Path | None = None  # served at /<token>/file/<id>/<name>
    content: str | None = None  # html / markdown / text
    choices: list[str] = field(default_factory=list)  # ask: buttons; empty = free text
    answer: str | None = None

    def public(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "title": self.title}
        if self.path is not None:
            d["url"] = f"file/{self.id}/{self.path.name}"
            d["name"] = self.path.name
        if self.content is not None:
            d["content"] = self.content
        if self.kind == "ask":
            d.update(choices=self.choices, answer=self.answer)
        return d


def kind_for(path: Path) -> str:
    return KINDS.get(path.suffix.lower(), "file")


class Viewer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 open_browser: Callable[[str], object] = webbrowser.open):
        self.host, self.port = host, port
        self.token = secrets.token_urlsafe(16)
        self.items: list[Item] = []
        self._ids = itertools.count(1)
        self._clients: set[web.WebSocketResponse] = set()
        self._answers: dict[str, threading.Event] = {}
        self._cancel = threading.Event()
        self._open_browser = open_browser
        self._opened = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="viewer", daemon=True)
        self._runner: web.AppRunner | None = None

    # -- lifecycle -------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/{self.token}/"

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._runner is not None

    def start(self) -> str:
        if not self.running:
            self._thread.start()
            self._call(self._start())
        return self.url

    def close(self) -> None:
        if not self.running:
            return
        self.cancel_asks()
        try:
            self._call(self._stop(), timeout=5)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._runner = None

    def _call(self, coro, timeout: float = 10):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _start(self) -> None:
        app = web.Application()
        t = self.token
        app.router.add_get(f"/{t}/", self._page)
        app.router.add_get(f"/{t}/ws", self._socket)
        app.router.add_get(f"/{t}/file/{{id}}/{{name}}", self._file)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # actual port when 0 was asked

    async def _stop(self) -> None:
        for ws in list(self._clients):
            await ws.close()
        await self._runner.cleanup()

    # -- HTTP ------------------------------------------------------------
    async def _page(self, request: web.Request) -> web.Response:
        return web.Response(text=PAGE.read_text(), content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def _file(self, request: web.Request) -> web.StreamResponse:
        path = self._presented_file(request.match_info["id"], request.match_info["name"])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    def _presented_file(self, item_id: str, name: str) -> Path | None:
        """The file of a presented item, only if the URL names it exactly and it still exists."""
        item = next((i for i in self.items if i.id == item_id), None)
        path = item.path if item else None
        matches = path is not None and path.name == name
        return path if matches and path.is_file() else None

    async def _socket(self, request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Origin") != self.origin:
            raise web.HTTPForbidden(text="wrong origin")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.add(ws)
        try:
            await ws.send_json({"type": "hello", "items": [i.public() for i in self.items]})
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_message(msg.data)
        finally:
            self._clients.discard(ws)
        return ws

    async def _on_message(self, data: str) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if msg.get("type") != "answer":
            return
        item = next((i for i in self.items if i.id == msg.get("id") and i.kind == "ask"), None)
        if item is None or item.answer is not None:
            return
        item.answer = str(msg.get("value", ""))[:10_000]
        await self._broadcast({"type": "answered", "id": item.id, "answer": item.answer})
        done = self._answers.get(item.id)
        if done:
            done.set()

    async def _broadcast(self, msg: dict) -> None:
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except (ConnectionError, RuntimeError):
                self._clients.discard(ws)

    async def _add(self, item: Item) -> None:
        self.items.append(item)
        await self._broadcast({"type": "item", "item": item.public()})

    # -- agent side (worker thread) --------------------------------------
    def present(self, item_kind: str, *, title: str = "", path: Path | None = None,
                content: str | None = None) -> Item:
        self.start()
        item = Item(str(next(self._ids)), item_kind, title, path, content)
        self._call(self._add(item))
        self.reveal()
        return item

    def ask(self, question: str, choices: list[str] | None = None, timeout: float = ASK_TIMEOUT) -> str:
        """Show a question and block until it is answered in the browser."""
        self.start()
        self._cancel.clear()
        item = Item(str(next(self._ids)), "ask", question, choices=list(choices or []))
        done = threading.Event()
        self._answers[item.id] = done
        self._call(self._add(item))
        self.reveal()
        try:
            for _ in range(int(timeout * 4)):
                if done.wait(0.25):
                    return item.answer or ""
                if self._cancel.is_set():
                    raise AskCancelled("interrupted")
            raise AskCancelled(f"no answer within {timeout:.0f}s")
        finally:
            self._answers.pop(item.id, None)

    def cancel_asks(self) -> None:
        self._cancel.set()

    def reveal(self) -> None:
        """Open the browser the first time something is shown, unless a viewer is already connected."""
        if not self._opened and not self._clients:
            self._opened = True
            self._open_browser(self.url)
