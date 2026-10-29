"""Issue #3229 and explicit web-toggle regressions.

Bug: allow_bash and allow_web_search were only read from form_data, so JSON
API callers (Content-Type: application/json) always had bash disabled.

Fix: (1) Read from JSON body as fallback.
     (2) Keep bash on the privilege fallback when unset.
     (3) Require an explicit per-turn web setting before exposing web tools.
"""

import ast
from pathlib import Path

import pytest

from src.action_intents import classify_tool_intent
from src.tool_policy import (
    WEB_TOOL_NAMES,
    is_web_search_explicitly_denied,
    web_search_enabled_for_turn,
)

_CHAT_ROUTES = Path(__file__).resolve().parent.parent / "routes" / "chat_routes.py"
_CHAT_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "chat.js"


# ── Source-level guards ─────────────────────────────────────────


def test_allow_web_search_reads_from_body_as_fallback():
    """chat_stream must read allow_web_search from the JSON body, not just form_data."""
    source = _CHAT_ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(source)

    chat_stream_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat_stream":
            chat_stream_func = node
            break
    assert chat_stream_func is not None

    found_body_fallback = False
    for node in ast.walk(chat_stream_func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "allow_web_search":
                    src_segment = ast.get_source_segment(source, node)
                    if src_segment and "body" in src_segment:
                        found_body_fallback = True
    assert found_body_fallback, (
        "allow_web_search assignment in chat_stream must fall back to JSON body"
    )


def test_browser_form_followups_include_approval_and_send_phrases():
    """Short approval replies after a form/browser turn must keep browser tools available."""
    source = _CHAT_ROUTES.read_text(encoding="utf-8")
    assert "approved" in source
    assert "proceed" in source
    assert "send(?:\\s+it)?" in source
    assert "submit(?:\\s+it)?" in source


def test_agent_loop_expands_browser_mcp_tools_from_connected_server():
    """Browser intent must not depend on stale hardcoded Playwright tool names."""
    source = (Path(__file__).resolve().parent.parent / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "def _expand_browser_mcp_tools" in source
    assert "server_id\") == \"builtin_browser\"" in source
    assert "_relevant_tools = _expand_browser_mcp_tools(_relevant_tools, mcp_mgr)" in source


def _build_disabled_tools(
    allow_web_search=None,
    use_web=None,
    can_use_browser=True,
    explicit_web_intent=False,
    global_disabled=None,
):
    """Replicate the disabled-tools logic from chat_stream for unit testing.

    Returns the set of tool names that would be disabled.
    """
    disabled_tools = set()

    search_enabled = web_search_enabled_for_turn(allow_web_search, use_web)
    if is_web_search_explicitly_denied(allow_web_search) or not search_enabled:
        disabled_tools.update(WEB_TOOL_NAMES)
    if explicit_web_intent:
        disabled_tools.update({
            "search_chats", "manage_skills", "manage_memory",
            "create_document", "edit_document", "update_document",
            "send_email", "reply_to_email",
            "manage_notes", "manage_calendar", "manage_tasks",
            "api_call", "builtin_browser",
        })
        if search_enabled:
            disabled_tools.difference_update(WEB_TOOL_NAMES)
        else:
            disabled_tools.update(WEB_TOOL_NAMES)
    elif search_enabled:
        disabled_tools.difference_update(WEB_TOOL_NAMES)

    # Enforce per-user privileges
    if not can_use_browser:
        disabled_tools.add("builtin_browser")
    if global_disabled and isinstance(global_disabled, list):
        disabled_tools.update(global_disabled)

    return disabled_tools


def test_json_body_allow_web_search_true_enables_web():
    """API caller sending {"allow_web_search": true} gets web tools enabled."""
    disabled = _build_disabled_tools(allow_web_search="true")
    assert "web_search" not in disabled
    assert "web_fetch" not in disabled


def test_json_body_allow_web_search_false_disables_web():
    """API caller sending {"allow_web_search": false} gets web tools disabled."""
    disabled = _build_disabled_tools(allow_web_search="false")
    assert "web_search" in disabled
    assert "web_fetch" in disabled


def test_chat_mode_use_web_true_enables_web():
    """Chat pre-search sends use_web=true as the explicit web setting."""
    disabled = _build_disabled_tools(use_web="true")
    assert "web_search" not in disabled
    assert "web_fetch" not in disabled


def test_allow_web_search_false_wins_over_use_web_true():
    """The agent web toggle hard-denies web even if another path says use_web=true."""
    disabled = _build_disabled_tools(allow_web_search="false", use_web="true")
    assert "web_search" in disabled
    assert "web_fetch" in disabled


@pytest.mark.parametrize(
    "message",
    [
        "please use web search for current CVEs",
        "search the web for current CVEs",
        "can you look up the latest docs",
    ],
)
def test_explicit_false_disables_web_despite_prompt_web_intent(message):
    """Explicit allow_web_search=false is a hard deny even when the prompt
    asks for web search."""
    intent = classify_tool_intent(message)
    assert intent is not None
    assert intent.category == "web"

    disabled = _build_disabled_tools(
        allow_web_search="false",
        explicit_web_intent=True,
    )
    assert "web_search" in disabled
    assert "web_fetch" in disabled


def test_prompt_web_intent_does_not_enable_web_without_setting():
    """Prompt-derived web intent alone must not expose web tools."""
    intent = classify_tool_intent("look up the latest docs")
    assert intent is not None
    assert intent.category == "web"

    disabled = _build_disabled_tools(
        allow_web_search=None,
        use_web=None,
        explicit_web_intent=True,
    )
    assert "web_search" in disabled
    assert "web_fetch" in disabled


def test_web_search_disabled_by_default_without_explicit_turn_setting():
    """Missing web settings must not expose web tools by default."""
    disabled = _build_disabled_tools(allow_web_search=None)
    assert "web_search" in disabled
    assert "web_fetch" in disabled


def test_global_disabled_web_wins_over_explicit_web_enable():
    """Admin-level disabled tools are still a hard deny."""
    disabled = _build_disabled_tools(
        allow_web_search="true",
        global_disabled=["web_search", "web_fetch"],
    )
    assert "web_search" in disabled
    assert "web_fetch" in disabled


def test_frontend_sends_explicit_allow_web_search_false_in_agent_mode():
    """chat.js must send allow_web_search=false when web toggle is off in agent mode."""
    source = _CHAT_JS.read_text(encoding="utf-8")
    assert "fd.append('allow_web_search', el('web-toggle').checked ? 'true' : 'false')" in source, (
        "Frontend must send explicit allow_web_search=false in agent mode when toggle is off"
    )
