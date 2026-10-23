"""Regression coverage for the built-in MCP servers' SDK compatibility line."""

from importlib.metadata import version
from pathlib import Path

from mcp.server import Server


REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"
MCP_PIN = "mcp==1.30.0"


def test_mcp_requirement_pins_v1_sdk():
    requirements = [
        line.split("#", 1)[0].strip().replace(" ", "")
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    ]

    assert MCP_PIN in requirements


def test_installed_mcp_supports_v1_api():
    """The built-in servers register through the v1 decorator shim."""
    assert hasattr(Server, "list_tools"), (
        f"installed mcp {version('mcp')} does not provide the v1 list_tools decorator"
    )
    assert hasattr(Server, "call_tool"), (
        f"installed mcp {version('mcp')} does not provide the v1 call_tool decorator"
    )
