"""Persistent MCP client used by the web dashboard (server.py).

server.py normally calls the approval workflow's Python functions
(src.actions.approval) directly — the same functions src/mcp_server/server.py
calls for an external AI agent such as Claude Code or Claude Desktop. For the
"Local files (CSV)" target's raise/stage/commit/rollback steps specifically,
the dashboard instead goes through the MCP server itself, over the real MCP
protocol, so those actions genuinely travel through MCP rather than being a
second, parallel caller of the same function — see server.py's
_handle_raise/_handle_stage/_handle_commit/_handle_rollback.

One client, one subprocess, for this process's lifetime: mcp_server.py is
spawned once — the same command ``.mcp.json`` gives Claude Code/Desktop —
and kept open on a dedicated background thread running its own asyncio event
loop. HTTP handler threads (server.py runs a synchronous
``ThreadingHTTPServer``, not asyncio) call the synchronous ``call_tool()``
below, which submits the request onto that loop and blocks for the result.

If the MCP server is unreachable, ``call_tool()`` raises ``McpUnavailableError``
— callers must surface that as an error, never silently fall back to calling
the underlying function directly, or the whole point of routing through MCP
is defeated.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .utils.logger import get_logger

log = get_logger("mcp_client")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MCP_SERVER_SCRIPT = _PROJECT_ROOT / "mcp_server.py"


class McpUnavailableError(RuntimeError):
    """The MCP server isn't reachable — not started yet, crashed, or timed
    out. Callers must treat this as a request failure, not fall back to
    calling the underlying approval function directly."""


def _first_text(result) -> Optional[str]:
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


class _McpClient:
    """Owns the background event-loop thread and the one long-lived
    ClientSession. Every method here is safe to call from any thread."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Optional[ClientSession] = None
        self._ready = threading.Event()
        self._start_error: Optional[BaseException] = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="mcp-client", daemon=True).start()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        except BaseException as exc:  # noqa: BLE001 - surfaced via call_tool()
            log.warning("MCP client failed to connect: %s", exc)
            self._start_error = exc
            self._ready.set()
            return
        self._loop.run_forever()

    async def _connect(self) -> None:
        # Kept open (never exited) for this client's lifetime — same
        # subprocess-over-stdio setup .mcp.json gives Claude Code/Desktop.
        stack = AsyncExitStack()
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(_MCP_SERVER_SCRIPT)],
            cwd=str(_PROJECT_ROOT),
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        self._stack = stack
        log.info("MCP client connected to %s", _MCP_SERVER_SCRIPT.name)
        self._ready.set()

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 20.0) -> dict:
        if not self._ready.wait(timeout=max(timeout, 15.0)):
            raise McpUnavailableError("MCP server did not become ready in time.")
        if self._start_error is not None or self._session is None or self._loop is None:
            raise McpUnavailableError(f"MCP server is unavailable: {self._start_error}")
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop
        )
        try:
            result = future.result(timeout=timeout)
        except Exception as exc:
            raise McpUnavailableError(f"MCP call to '{name}' failed: {exc}") from exc

        text = _first_text(result)
        if result.isError:
            raise McpUnavailableError(f"MCP tool '{name}' returned an error: {text}")
        if text is None:
            raise McpUnavailableError(f"MCP tool '{name}' returned no content.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpUnavailableError(f"MCP tool '{name}' returned unparseable content: {exc}") from exc


_client = _McpClient()


def start() -> None:
    """Call once at server startup. Non-blocking — the subprocess launch and
    MCP handshake happen on a background thread, so a slow-starting MCP
    process only delays the *first* call that needs it, never the web
    server's own startup."""
    _client.start()


def call_tool(name: str, arguments: dict[str, Any], timeout: float = 20.0) -> dict:
    return _client.call_tool(name, arguments, timeout)
