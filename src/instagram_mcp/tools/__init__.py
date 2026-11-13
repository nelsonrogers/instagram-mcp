"""MCP tool definitions, grouped by area. Every tool is read-only."""

from __future__ import annotations

from mcp.server import MCPServer

from instagram_mcp.context import AppContext
from instagram_mcp.tools import account, analytics, content, discovery, report


def register_all(mcp: MCPServer, ctx: AppContext) -> None:
    account.register(mcp, ctx)
    analytics.register(mcp, ctx)
    content.register(mcp, ctx)
    discovery.register(mcp, ctx)
    report.register(mcp, ctx)
