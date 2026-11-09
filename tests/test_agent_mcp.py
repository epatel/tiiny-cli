import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tai.agent import mcp_client as M
from tai.agent.tools import ToolContext, ToolError

ECHO = Path(__file__).parent / "fixtures" / "echo_mcp.py"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".config" / "tai").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: h)
    return h


def write(path, servers):
    path.write_text(json.dumps({"mcpServers": servers}))


def test_config_merges_user_and_project_and_expands_vars(home, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN", "s3cret")
    monkeypatch.delenv("MISSING", raising=False)
    write(home / ".config" / "tai" / "mcp.json", {
        "shared": {"command": "user-cmd"},
        "web": {"type": "http", "url": "https://x/${MISSING:-mcp}", "headers": {"Authorization": "Bearer ${TOKEN}"}}})
    project = tmp_path / "proj"
    project.mkdir()
    write(project / ".mcp.json", {"shared": {"command": ".venv/bin/srv", "args": ["--k", "${TOKEN}"], "env": {"A": "1"}}})
    servers, problems = M.load_config(project)
    assert problems == []
    assert servers["shared"] == M.ServerConfig("shared", ".venv/bin/srv", ("--k", "s3cret"), {"A": "1"})
    assert servers["web"].transport == "http" and servers["web"].url == "https://x/mcp"
    assert servers["web"].headers == {"Authorization": "Bearer s3cret"}


def test_config_problems_are_reported_not_raised(home, tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    write(home / ".config" / "tai" / "mcp.json", {"bad": {"args": ["x"]}})
    servers, problems = M.load_config(tmp_path)
    assert servers == {} and len(problems) == 2 and "needs a command or a url" in " ".join(problems)


def test_names_paths_and_results():
    assert M.tool_name("my.server", "do-it/now") == "mcp__my_server__do-it_now"
    assert len(M.tool_name("s" * 40, "t" * 40)) == 64
    assert M.command_path(Path("/p"), ".venv/bin/x") == "/p/.venv/bin/x"
    assert M.command_path(Path("/p"), "npx") == "npx" and M.command_path(Path("/p"), "/usr/bin/y") == "/usr/bin/y"
    text = SimpleNamespace(type="text", text="hi")
    img = SimpleNamespace(type="image", mime_type="image/png")
    assert M.result_text(SimpleNamespace(content=[text, img])) == "hi\n[image: image/png]"
    assert M.result_text(SimpleNamespace(content=[], structured_content={"a": 1})) == '{\n  "a": 1\n}'


def test_agent_tools_permissions_from_read_only_hint():
    hub = M.McpHub({}, Path("/p"), Path("/tmp"))
    state = M.ServerState(M.ServerConfig("srv", "x"), session=object(), tools=[
        M.RemoteTool("srv", "look", "Look", {"type": "object", "properties": {"q": {"type": "string"}}}, True),
        M.RemoteTool("srv", "write", "Write", {}, False)])
    hub.states = {"srv": state}
    tools = M.agent_tools(hub)
    look, wr = tools["mcp__srv__look"], tools["mcp__srv__write"]
    assert look.grant is None and look.preview is None
    assert wr.grant == "mcp:srv" and wr.preview({"x": 1}, None).body == '{\n  "x": 1\n}'
    assert look.spec()["function"]["parameters"]["properties"] == {"q": {"type": "string"}}
    assert wr.spec()["function"]["parameters"] == {"type": "object", "properties": {}}
    assert look.summary({"q": "z"}) == "srv · look"


def test_hub_end_to_end_with_a_real_stdio_server(tmp_path):
    servers = {"echo": M.ServerConfig("echo", sys.executable, (str(ECHO),)),
               "broken": M.ServerConfig("broken", "/nonexistent/server")}
    hub = M.McpHub(servers, tmp_path, tmp_path / "logs")
    hub.start()
    try:
        assert [s.config.name for s in hub.connected()] == ["echo"]
        assert hub.failed()[0].config.name == "broken" and hub.failed()[0].error
        tools = M.agent_tools(hub)
        assert set(tools) == {"mcp__echo__echo", "mcp__echo__add", "mcp__echo__boom"}
        assert tools["mcp__echo__echo"].grant is None and tools["mcp__echo__add"].grant == "mcp:echo"
        ctx = ToolContext(tmp_path)
        assert tools["mcp__echo__echo"].run({"text": "hello"}, ctx) == "hello"
        assert tools["mcp__echo__add"].run({"a": 2, "b": 3}, ctx) == "5"
        with pytest.raises(ToolError, match="Error executing tool boom"):  # server hides the detail
            tools["mcp__echo__boom"].run({}, ctx)
        with pytest.raises(ToolError, match="not connected"):
            hub.call("broken", "x", {})
        assert "Echo server for tests." in M.prompt_section(hub)
    finally:
        hub.close()
    assert "echo called" in (tmp_path / "logs" / "mcp-echo.log").read_text()


def test_describe_unwraps_exception_groups():
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [OSError("refused")])])
    assert M._describe(group) == "OSError: refused"
    assert M._describe(ValueError()) == "ValueError"
