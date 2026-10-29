"""Real stdio protocol coverage for the four built-in MCP servers.

These tests start each server as a subprocess and drive it through a real
``ClientSession``. They deliberately call ``tools/call``: a server can connect
and answer ``tools/list`` while every tool invocation fails request validation,
which is exactly the v2 regression this file guards against.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVERS = REPO_ROOT / "mcp_servers"


async def _list_and_call(script: str, tool: str, arguments: dict, data_dir: Path) -> str:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "ODYSSEUS_DATA_DIR": str(data_dir),
        "DATABASE_URL": f"sqlite:///{data_dir / 'app.db'}",
        "ODYSSEUS_MCP_MEMORY_OWNER": "protocol-test",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVERS / script)],
        env=env,
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert tool in {item.name for item in tools.tools}
            result = await session.call_tool(tool, arguments)
            return "\n".join(
                block.text for block in result.content if getattr(block, "text", None)
            )


@pytest.mark.parametrize(
    ("script", "tool", "arguments", "expected"),
    [
        ("memory_server.py", "manage_memory", {"action": "list"}, None),
        ("rag_server.py", "manage_rag", {"action": "list"}, None),
        ("email_server.py", "list_email_accounts", {}, None),
    ],
)
async def test_builtin_server_completes_tools_call(
    tmp_path: Path, script: str, tool: str, arguments: dict, expected: str | None
):
    text = await _list_and_call(script, tool, arguments, tmp_path)
    assert text, "tools/call returned no text content"
    if expected is not None:
        assert expected in text.lower()
