"""MCP servers for `tai agent`: config loading, connections, and agent tools.

Config uses Claude Code's format: `{"mcpServers": {name: {"command", "args", "env"}
| {"type": "http", "url", "headers"}}}`, read from `~/.config/tai/mcp.json` and then
the project's `.mcp.json` (a project entry overrides a user one with the same name).
`${VAR}` and `${VAR:-default}` are expanded from the environment.

Sessions live on one background asyncio loop (``McpHub``); the agent's worker
thread calls tools through ``run_coroutine_threadsafe``. Each server's stderr goes
to a log file, never the terminal (it would garble the TUI).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

from tai.agent.tools import Preview, Tool, ToolContext, ToolError

CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 600.0
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


def config_paths(root: Path) -> list[Path]:
    return [Path.home() / ".config" / "tai" / "mcp.json", root / ".mcp.json"]


def expand(value: str) -> str:
    return _VAR.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)


def _server(name: str, d: dict) -> ServerConfig:
    env = {k: expand(str(v)) for k, v in (d.get("env") or {}).items()}
    headers = {k: expand(str(v)) for k, v in (d.get("headers") or {}).items()}
    if d.get("url"):
        return ServerConfig(name, url=expand(d["url"]), headers=headers)
    if not d.get("command"):
        raise ValueError(f"MCP server '{name}' needs a command or a url")
    return ServerConfig(name, expand(d["command"]), tuple(expand(str(a)) for a in d.get("args") or []), env)


def load_config(root: Path) -> tuple[dict[str, ServerConfig], list[str]]:
    """Returns (servers by name, problems found while reading the config files)."""
    servers: dict[str, ServerConfig] = {}
    problems: list[str] = []
    for path in config_paths(root):
        if not path.is_file():
            continue
        try:
            entries = json.loads(path.read_text()).get("mcpServers") or {}
            for name, d in entries.items():
                servers[name] = _server(name, d)
        except (ValueError, AttributeError) as e:
            problems.append(f"{path}: {e}")
    return servers, problems


@dataclass(frozen=True)
class RemoteTool:
    server: str
    name: str
    description: str
    schema: dict
    read_only: bool


@dataclass
class ServerState:
    config: ServerConfig
    tools: list[RemoteTool] = field(default_factory=list)
    instructions: str = ""
    error: str | None = None
    session: object = None  # mcp.ClientSession while connected


def _content_text(c) -> str:
    kind = getattr(c, "type", "")
    if kind == "text":
        return c.text
    if kind == "resource" and hasattr(c.resource, "text"):
        return c.resource.text
    return f"[{kind or 'content'}: {getattr(c, 'mime_type', '') or 'binary'}]"


def result_text(result) -> str:
    """Flatten an MCP CallToolResult into text for the model."""
    parts = [_content_text(c) for c in getattr(result, "content", None) or []]
    if not parts and getattr(result, "structured_content", None) is not None:
        parts.append(json.dumps(result.structured_content, indent=2))
    return "\n".join(parts) or "(no output)"


def command_path(root: Path, command: str) -> str:
    """Relative paths with a slash (`.venv/bin/x`) are relative to the project root;
    bare names (`npx`) are looked up on PATH."""
    return str(root / command) if "/" in command and not Path(command).is_absolute() else command


class McpHub:
    def __init__(self, servers: dict[str, ServerConfig], root: Path, log_dir: Path):
        self.root, self.log_dir = root, log_dir
        self.states = {name: ServerState(cfg) for name, cfg in servers.items()}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp", daemon=True)
        self._stacks: dict[str, AsyncExitStack] = {}

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Connect every server (in parallel); failures are recorded in ``states[...].error``."""
        if not self.states:
            return
        self._thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._connect_all(), self._loop)
        fut.result(timeout=CONNECT_TIMEOUT + 5)

    async def _connect_all(self) -> None:
        await asyncio.gather(*(self._connect(s) for s in self.states.values()))

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        fut = asyncio.run_coroutine_threadsafe(self._close_all(), self._loop)
        try:
            fut.result(timeout=10)
        except Exception:  # noqa: BLE001 — best effort on shutdown
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    async def _close_all(self) -> None:
        for stack in self._stacks.values():
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def _connect(self, state: ServerState) -> None:
        try:
            await asyncio.wait_for(self._open(state), CONNECT_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — any failure just disables that server
            state.error = _describe(e)

    async def _open(self, state: ServerState) -> None:
        from mcp import ClientSession

        stack = AsyncExitStack()
        self._stacks[state.config.name] = stack
        read, write = await stack.enter_async_context(self._transport(state.config, stack))
        session = await stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
        state.instructions = (getattr(init, "instructions", None) or "").strip()
        state.tools = await self._list_tools(state.config.name, session)
        state.session = session

    def _transport(self, cfg: ServerConfig, stack: AsyncExitStack):
        if cfg.transport == "http":
            import httpx2
            from mcp.client.streamable_http import streamable_http_client

            client = httpx2.AsyncClient(headers=cfg.headers, timeout=CALL_TIMEOUT)
            stack.push_async_callback(client.aclose)
            return _two_streams(streamable_http_client(cfg.url, http_client=client))
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.log_dir / f"mcp-{cfg.name}.log", "a")  # noqa: SIM115 — closed by the stack
        stack.callback(log.close)
        params = StdioServerParameters(command=command_path(self.root, cfg.command), args=list(cfg.args), env={**os.environ, **cfg.env},
                                       cwd=str(self.root))
        return stdio_client(params, errlog=log)

    @staticmethod
    async def _list_tools(server: str, session) -> list[RemoteTool]:
        tools, cursor = [], None
        while True:
            res = await session.list_tools(cursor) if cursor else await session.list_tools()
            for t in res.tools:
                ro = bool(t.annotations and t.annotations.read_only_hint)
                tools.append(RemoteTool(server, t.name, t.description or "", t.input_schema or {}, ro))
            cursor = getattr(res, "next_cursor", None)
            if not cursor:
                return tools

    # -- use -------------------------------------------------------------
    def call(self, server: str, tool: str, args: dict) -> str:
        state = self.states[server]
        if state.session is None:
            raise ToolError(f"MCP server '{server}' is not connected: {state.error}")
        fut = asyncio.run_coroutine_threadsafe(state.session.call_tool(tool, args), self._loop)
        try:
            result = fut.result(timeout=CALL_TIMEOUT)
        except TimeoutError as e:
            fut.cancel()
            raise ToolError(f"MCP tool {server}/{tool} timed out after {CALL_TIMEOUT:.0f}s") from e
        except Exception as e:  # noqa: BLE001 — transport errors become tool errors for the model
            raise ToolError(f"MCP call failed: {_describe(e)}") from e
        text = result_text(result)
        if getattr(result, "is_error", False):
            raise ToolError(text)
        return text

    def connected(self) -> list[ServerState]:
        return [s for s in self.states.values() if s.session is not None]

    def failed(self) -> list[ServerState]:
        return [s for s in self.states.values() if s.error]


def _describe(e: BaseException) -> str:
    """Innermost message of an exception (group), e.g. from anyio task groups.

    Duck-typed, not `isinstance(e, BaseExceptionGroup)`: that builtin is 3.11+, and on
    3.10 anyio raises the `exceptiongroup` backport instead."""
    while isinstance(getattr(e, "exceptions", None), tuple) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


class _two_streams:
    """Adapt a transport yielding (read, write, *extra) to yield just (read, write)."""

    def __init__(self, cm):
        self.cm = cm

    async def __aenter__(self):
        streams = await self.cm.__aenter__()
        return streams[0], streams[1]

    async def __aexit__(self, *exc):
        return await self.cm.__aexit__(*exc)


# ------------------------------------------------------------------ agent tools
def tool_name(server: str, tool: str) -> str:
    """`mcp__server__tool`, limited to what OpenAI function names allow."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")[:64]


def agent_tools(hub: McpHub) -> dict[str, Tool]:
    tools: dict[str, Tool] = {}
    for state in hub.connected():
        for rt in state.tools:
            tools[tool_name(rt.server, rt.name)] = _wrap(hub, rt)
    return tools


def _wrap(hub: McpHub, rt: RemoteTool) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        return hub.call(rt.server, rt.name, args)

    def preview(args: dict, ctx: ToolContext) -> Preview:
        return Preview(f"MCP {rt.server} · {rt.name}", json.dumps(args, indent=2), "json")

    schema = {"type": "object", "properties": {}, **rt.schema}
    return Tool(tool_name(rt.server, rt.name), f"[MCP {rt.server}] {rt.description}"[:1024], {}, run,
                None if rt.read_only else preview, grant=None if rt.read_only else f"mcp:{rt.server}",
                schema=schema, display=f"{rt.server} · {rt.name}")


def prompt_section(hub: McpHub) -> str:
    notes = [f"## {s.config.name}\n{s.instructions[:2000]}" for s in hub.connected() if s.instructions]
    if not notes:
        return ""
    return "\n# MCP servers\n\nTools named mcp__<server>__<tool> come from these servers:\n\n" + "\n\n".join(notes) + "\n"
