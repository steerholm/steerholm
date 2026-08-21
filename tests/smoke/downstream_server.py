"""Minimal stdio MCP server used by the smoke scenario.

Self-contained (no Node/npx, no network): exposes a small, predictable tool
surface so the scenario can assert allowed/denied/argument-policy behavior.
Run as: python downstream_server.py
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("smoke-downstream")


@mcp.tool()
def echo(message: str) -> str:
    """Return the message back, prefixed, so the scenario can assert on it."""
    return f"echo:{message}"


@mcp.tool()
def secret() -> str:
    """A tool the scenario denies via policy; calling it should be rejected."""
    return "secret-data"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Used to exercise argument policies (e.g. a must be a positive integer)."""
    return a + b


if __name__ == "__main__":
    mcp.run()
