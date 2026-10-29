"""Pin the reduced userspace: no shell, filesystem, image, or Cookbook tools."""

import src.agent_tools as agent_tools
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
from src.tool_parsing import _TOOL_NAME_MAP
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

REMOVED = {
    "bash",
    "python",
    "read_file",
    "write_file",
    "edit_file",
    "apply_patch",
    "grep",
    "glob",
    "ls",
    "todowrite",
    "manage_bg_jobs",
    "edit_image",
    "generate_image",
    "download_model",
    "serve_model",
    "serve_preset",
    "list_serve_presets",
    "list_cookbook_servers",
    "list_served_models",
    "stop_served_model",
    "tail_serve_output",
    "list_downloads",
    "cancel_download",
    "search_hf_models",
    "list_cached_models",
    "adopt_served_model",
}


def _schema_names():
    return {
        (schema.get("function") or {}).get("name")
        for schema in FUNCTION_TOOL_SCHEMAS
    }


def test_removed_tools_have_no_schema_handler_or_alias():
    assert _schema_names().isdisjoint(REMOVED)
    assert set(agent_tools.TOOL_HANDLERS).isdisjoint(REMOVED)
    assert agent_tools.TOOL_TAGS.isdisjoint(REMOVED)
    assert set(BUILTIN_TOOL_DESCRIPTIONS).isdisjoint(REMOVED)
    assert set(_TOOL_NAME_MAP).isdisjoint(REMOVED)
    assert set(_TOOL_NAME_MAP.values()).isdisjoint(REMOVED)


def test_read_upload_is_the_only_file_reader():
    assert "read_upload" in _schema_names()
    assert "read_upload" in agent_tools.TOOL_HANDLERS
    assert "read_upload" in agent_tools.TOOL_TAGS


def test_expected_userspace_surface():
    names = _schema_names()
    assert {
        "list_library_files",
        "read_library_file",
        "make_library_file",
        "create_document",
        "edit_document",
        "update_document",
        "suggest_document",
        "manage_documents",
        "list_sessions",
        "manage_session",
        "search_chats",
        "ask_user",
        "update_plan",
    } <= names
