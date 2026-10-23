"""Register the built-in MCP servers with the MCP SDK v1 Server API.

The four built-in stdio servers keep their full-request handlers
(``handle_list_tools(ctx, request)`` / ``handle_call_tool(ctx, request)``) so
their existing logic and direct-call tests stay unchanged. The MCP SDK v1
decorator API passes a request to ``list_tools`` handlers and
``(name, arguments)`` to ``call_tool`` handlers, so this shim reconstructs the
request objects those handlers expect.

This module is intentionally v1-only. It must not be imported when the app is
running against the MCP SDK v2 line.
"""

from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    ListToolsRequest,
)


def register_v1_handlers(server, list_handler, call_handler):
    """Register the full-request handlers through the SDK v1 decorators."""

    @server.list_tools()
    async def _list_tools():
        return await list_handler(None, ListToolsRequest(method="tools/list"))

    @server.call_tool()
    async def _call_tool(name, arguments):
        request = CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name=name, arguments=arguments or {}),
        )
        return await call_handler(None, request)
