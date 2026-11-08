"""`tai serve`: let other machines use the device through this one.

The server forwards the device routes tai uses (``/v1/*`` and ``/api/v1/*``) to
the device. It checks the caller's ``Authorization: Bearer <TAI_API_KEY>`` and
sends the device key instead, so clients never see the device key. Responses
are streamed through unchanged (SSE chat, load progress, large GLB downloads),
so a client is just a ``TiinyClient`` with a different base URL and key.

It listens on 127.0.0.1 only; nginx on the same machine gives it its external URL,
e.g. ``https://host/tai`` -> ``http://127.0.0.1:8798``. Whatever prefix nginx leaves
in the path is ignored: the route starts at the first ``/v1/`` or ``/api/v1/``.
``GET …/healthz`` answers without a key, for nginx or monitoring.
"""

from __future__ import annotations

import hmac
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp
from aiohttp import web

from tai.config import Settings

FORWARDED_PREFIXES = ("/api/v1/", "/v1/")
# Not passed on in either direction: per-connection headers, and the two keys.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "host", "authorization", "x-api-key"}
MAX_REQUEST = 512 * 1024**2  # uploads (3D source images, audio for ASR)
DEVICE = web.AppKey("device", aiohttp.ClientSession)

Log = Callable[[str], None]


@dataclass(frozen=True)
class ServeConfig:
    device: Settings  # direct device settings (address + device key)
    api_key: str  # what clients must send


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": {"message": message}}, status=status)


def _authorized(request: web.Request, key: str) -> bool:
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(given) and hmac.compare_digest(given.encode(), key.encode())


def device_path(path: str) -> str | None:
    """The device route in ``path``, ignoring any prefix nginx left in front
    (``/tai/v1/x`` -> ``/v1/x``); ``/healthz`` for health checks; None for anything else."""
    if path.endswith("/healthz"):
        return "/healthz"
    for route in FORWARDED_PREFIXES:  # /api/v1/ first: it contains /v1/
        i = path.find(route)
        if i >= 0:
            return path[i:]
    return None


def _headers(headers, drop: set[str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def make_app(cfg: ServeConfig, log: Log = lambda line: print(line, file=sys.stderr)) -> web.Application:
    device_auth = {"Authorization": f"Bearer {cfg.device.token}"}

    async def session_ctx(app: web.Application):
        # No overall or read timeout: model loads, 3D stages and long answers take minutes.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
        app[DEVICE] = aiohttp.ClientSession(cfg.device.base_url, timeout=timeout, auto_decompress=False)
        yield
        await app[DEVICE].close()

    async def handle(request: web.Request) -> web.StreamResponse:
        path = device_path(request.path)
        if path == "/healthz":
            return web.json_response({"ok": True})
        if not _authorized(request, cfg.api_key):
            return _error(401, "Missing or wrong API key (TAI_API_KEY).")
        if path is None:
            return _error(404, f"tai serve only forwards {', '.join(FORWARDED_PREFIXES)}")
        started = time.monotonic()
        try:
            resp = await _forward(request, path, device_auth)
        except aiohttp.ClientError as e:
            log(f"{request.method} {path} -> device unreachable: {e}")
            return _error(502, f"tai serve can't reach the device at {cfg.device.device}: {e}")
        log(f"{request.method} {path} {resp.status} {time.monotonic() - started:.1f}s")
        return resp

    async def _forward(request: web.Request, path: str, auth: dict) -> web.StreamResponse:
        body = await request.read() if request.can_read_body else None
        headers = {**_headers(request.headers, HOP_BY_HOP | {"content-length"}), **auth}
        url = path + (f"?{request.query_string}" if request.query_string else "")
        async with request.app[DEVICE].request(request.method, url, data=body, headers=headers,
                                                 allow_redirects=False) as upstream:
            resp = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                      headers=_headers(upstream.headers, HOP_BY_HOP))
            await resp.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():  # as it arrives: SSE stays live
                    await resp.write(chunk)
                await resp.write_eof()
            except ConnectionResetError:
                # The client hung up mid-stream (normal: tai stops reading at [DONE], Ctrl+C,
                # Esc in the agent). Leaving the `async with` also ends the device request.
                log(f"{request.method} {path} client disconnected")
            return resp

    app = web.Application(client_max_size=MAX_REQUEST)
    app.cleanup_ctx.append(session_ctx)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


def run(cfg: ServeConfig, port: int, log: Log) -> None:
    log(f"tai serve on http://127.0.0.1:{port} -> device {cfg.device.device}")
    web.run_app(make_app(cfg, log), host="127.0.0.1", port=port, print=None, access_log=None)
