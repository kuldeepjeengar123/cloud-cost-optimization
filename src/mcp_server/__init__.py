"""AWS Cost & Ops MCP server — see ``server.py`` for the tool surface and
the safety model (every write goes through the same raise/stage/commit gate
the web dashboard uses)."""

from .server import mcp

__all__ = ["mcp"]
