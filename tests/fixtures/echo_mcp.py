"""Tiny stdio MCP server used by tests/test_agent_mcp.py."""

import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("echo", instructions="Echo server for tests.")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    """Return the text unchanged."""
    print("echo called", file=sys.stderr)  # must land in the log file, not the terminal
    return text


@server.tool()
def add(a: int, b: int) -> str:
    """Add two numbers (not marked read-only)."""
    return str(a + b)


@server.tool()
def boom() -> str:
    """Always fails."""
    raise RuntimeError("kaboom")


if __name__ == "__main__":
    server.run("stdio")
