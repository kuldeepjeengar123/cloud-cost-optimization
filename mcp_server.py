"""Convenience launcher for the AWS Cost & Ops MCP server.

    python mcp_server.py

Speaks MCP over stdio — point Claude Code, Claude Desktop, or any other MCP
client at this command (see .mcp.json). Every read tool is open; every write
tool funnels through the same raise -> stage -> commit approval gate the web
dashboard uses (src/mcp_server/server.py has the full tool list and the
safety model).
"""

from src.mcp_server.server import main

if __name__ == "__main__":
    main()
