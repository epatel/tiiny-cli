"""Wiring for `tai agent`: pick and load the model, connect MCP servers, find skills,
open the session, start the TUI.

Everything that may prompt or print (model loading and its "unload these models?"
question, MCP connection status) happens here, in the normal terminal, before the
full-screen UI takes over.
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass
from pathlib import Path

import click

from tai.agent import compact, mcp_client, skills
from tai.agent.loop import MAX_STEPS, Agent, AgentConfig, Decision, Transcript
from tai.agent.prompt import system_prompt
from tai.agent.tools import TOOLS, Tool, ToolContext
from tai.agent.media import MediaContext, media_tools
from tai.agent.web import web_tools
from tai.agent.tui import AgentApp, AppInfo, TuiLoaderUI
from tai.agent.viewer import Viewer
from tai.config import data_dir
from tai.models import Model
from tai.options import AgentRequest, SessionRef


@dataclass
class AgentSession:
    sid: int
    model: Model
    messages: list[dict]
    root: Path


@dataclass
class Extensions:
    """Skills and MCP servers available to this agent run."""
    tools: dict[str, Tool]
    prompt: str
    skills: list[skills.Skill]
    hub: mcp_client.McpHub | None

    def server_lines(self) -> list[str]:
        if self.hub is None:
            return []
        ok = [f"{s.config.name} — {len(s.tools)} tools" for s in self.hub.connected()]
        return ok + [f"{s.config.name} — failed: {s.error}" for s in self.hub.failed()]

    def close(self) -> None:
        if self.hub is not None:
            self.hub.close()


class SessionSink:
    """Where the agent's messages are saved. Phases and compaction move on to a new session
    (named after the phase / pointing back), so the earlier one stays complete."""

    def __init__(self, store, s: AgentSession):
        self.store, self.s, self.sid = store, s, s.sid

    def save(self, msg: dict) -> None:
        self.store.add_raw_message(self.sid, msg)

    def new(self, name: str, messages: list[dict] = ()) -> None:
        self.sid = self.store.create_agent_session(self.s.model.id, str(self.s.root))
        self.store.update_session(self.sid, name=name[:120])
        for msg in messages:
            self.save(msg)

    def compacted(self, messages: list[dict]) -> None:
        self.new(f"compacted from session {self.sid}", messages)


def compact_at(app) -> int:
    """`agent.compact`: off, or the % of the context window at which to compact (default 85)."""
    value = app.store.get_setting("agent.compact")
    if value is None:
        return compact.DEFAULT_AT
    return 0 if value == "off" else int(value)


def max_steps(app) -> int:
    """`agent.max_steps`: model calls per turn, or off (0: no limit). Default MAX_STEPS."""
    value = app.store.get_setting("agent.max_steps")
    if value is None:
        return MAX_STEPS
    return 0 if value == "off" else int(value)


def open_session(app, session: SessionRef, model: str | None, root: Path) -> AgentSession:
    """New session in ``root``, or resume one (-r id, or -c: latest in this folder)."""
    store = app.store
    if not session.resuming:
        m = app.agent_model(model)
        return AgentSession(store.create_agent_session(m.id, str(root)), m, [], root)
    row = store.find_session(session.resume) if session.resume else store.last_session("agent", str(root))
    if not row:
        raise click.UsageError("No agent session to resume" + ("" if session.resume else " in this folder."))
    if row["kind"] != "agent":
        raise click.UsageError(f"Session {row['id']} is a {row['kind']} session; use `tai {row['kind']} -r`.")
    return AgentSession(row["id"], app.agent_model(model or row["model"]), store.raw_messages(row["id"]), root)


def load_extensions(root: Path, use_mcp: bool = True) -> Extensions:
    tools = dict(TOOLS) | web_tools()
    found = skills.discover(root)
    if found:
        tools["skill"] = skills.skill_tool(found)
    hub = _connect_mcp(root) if use_mcp else None
    if hub is not None:
        tools.update(mcp_client.agent_tools(hub))
    prompt = skills.prompt_section(found) + (mcp_client.prompt_section(hub) if hub else "")
    return Extensions(tools, prompt, found, hub)


def _connect_mcp(root: Path) -> mcp_client.McpHub | None:
    servers, problems = mcp_client.load_config(root)
    for p in problems:
        click.secho(f"MCP config problem: {p}", fg="yellow", err=True)
    if not servers:
        return None
    click.echo(f"Connecting to MCP servers: {', '.join(servers)}…", err=True)
    hub = mcp_client.McpHub(servers, root, data_dir() / "logs")
    hub.start()
    for s in hub.failed():
        click.secho(f"  {s.config.name}: failed — {s.error} (log: {data_dir() / 'logs'})", fg="yellow", err=True)
    return hub


def _title(s: AgentSession, ext: Extensions) -> str:
    home = Path.home()
    where = f"~/{s.root.relative_to(home)}" if home in s.root.parents else str(s.root)
    resumed = f" · resumed ({len(s.messages)} messages)" if s.messages else ""
    extras = [f"{len(ext.skills)} skills"] if ext.skills else []
    extras += [f"{len(ext.hub.connected())} MCP"] if ext.hub and ext.hub.connected() else []
    tail = f" · {' · '.join(extras)}" if extras else ""
    return f"tai agent · {s.model.name} · {where} · session {s.sid}{resumed}{tail}"


def run_agent(app, req: AgentRequest) -> None:
    root = Path.cwd().resolve()
    app.unsaved(req.session)
    s = open_session(app, req.session, req.model, root)
    app.ensure_loaded(s.model)
    ext = load_extensions(root, use_mcp=not req.no_mcp)
    viewer = Viewer()  # starts on first use (show / ask / image / `/view`)
    try:
        media = MediaContext(app.client, viewer, agent_model=s.model.id)
        ext.tools.update(media_tools(media))
        ui = _build_app(app, s, ext, req)
        ui.agent.on_cancel.extend([viewer.cancel_asks, media.cancel.set])
        ui.info.open_viewer = lambda: _open_viewer(viewer)
        media.ui = TuiLoaderUI(ui)
        ui.run()
    finally:
        viewer.close()
        ext.close()


# Thinking for the agent: `agent.think`, else the general `think` (a --think flag beats both).
# Kept apart because reasoning that is fine in a chat can push agent steps past the device's
# 10-minute request limit (Qwen3.8-27B: 398 s for a fix that took 12 s without it).
AGENT_THINK_SETTINGS = ("agent.think", "think")


def _build_app(app, s: AgentSession, ext: Extensions, req: AgentRequest) -> AgentApp:
    from tai.cli import _think_extra  # shared with ask/chat

    think = _think_extra(app, s.model, req.think, settings=AGENT_THINK_SETTINGS)
    config = AgentConfig(s.model.id, system_prompt(s.root, s.model.name, ext.prompt), ToolContext(s.root),
                         extra=think, tools=ext.tools, context_window=s.model.context,
                         max_steps=max_steps(app))
    sink = SessionSink(app.store, s)
    transcript = Transcript(s.messages, on_message=sink.save, on_replace=sink.compacted)
    agent = Agent(app.client, config, lambda *a: Decision.DENY, transcript)  # the TUI installs its approver
    compact.Compactor(agent, compact_at(app))
    info = AppInfo(_title(s, ext), initial=None if req.phase else req.text,
                   phase_goal=req.text if req.phase else None, on_clear=lambda: app.store.clear_messages(sink.sid),
                   skills=[(k.name, k.description) for k in ext.skills], servers=ext.server_lines(),
                   history=app.store.prompts(s.root), on_prompt=lambda text: app.store.add_prompt(s.root, text),
                   context_window=s.model.context, can_think=bool(s.model.thinking.get("toggleable")),
                   on_new_context=sink.new)
    return AgentApp(agent, info)


def _open_viewer(viewer: Viewer) -> str:
    url = viewer.start()
    webbrowser.open(url)
    return url
