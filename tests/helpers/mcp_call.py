"""In-process invocation helpers for MCP servers' tool handlers.

The MCP server modules (mcp_servers/*_server.py) migrated to the raw
``mcp.server.Server`` v2 API: tools are dispatched through
``handle_call_tool(ctx, request)`` registered via ``add_request_handler``,
with no module-level ``call_tool`` anymore. Tests that exercise the server
logic in-process call these helpers instead of the removed ``call_tool``.

The returned value is the handler's ``CallToolResult.content`` — a list of
``mcp.types.TextContent`` — matching the old ``call_tool(...) -> list[TextContent]``
contract so existing ``result[0].text`` assertions keep working.
"""

from __future__ import annotations

import asyncio

from mcp.types import CallToolRequest, CallToolRequestParams, TextContent


def build_request(name: str, arguments: dict) -> CallToolRequest:
    """Build a ``tools/call`` request the server's ``handle_call_tool`` expects."""
    return CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )


async def call_tool_async(server_module, name: str, arguments: dict) -> list[TextContent]:
    """Invoke ``handle_call_tool`` in-process; return its text content list."""
    result = await server_module.handle_call_tool(None, build_request(name, arguments))
    return list(result.content)


def call_tool_sync(server_module, name: str, arguments: dict) -> list[TextContent]:
    """Sync wrapper around :func:`call_tool_async` for non-async tests."""
    return asyncio.run(call_tool_async(server_module, name, arguments))
