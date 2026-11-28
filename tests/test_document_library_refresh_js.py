"""Open Library must live-refresh after LLM file/folder changes.

These are source-structure assertions: the browser modules are not importable
in pytest (same approach as the sibling ``*_js.py`` suites).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFRESH = (ROOT / "static/js/libraryRefresh.js").read_text(encoding="utf-8")
LIB = (ROOT / "static/js/documentLibrary.js").read_text(encoding="utf-8")
CHAT = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
COMPARE = (ROOT / "static/js/compare/stream.js").read_text(encoding="utf-8")
GROUP = (ROOT / "static/js/group.js").read_text(encoding="utf-8")


def test_refresh_helper_targets_library_mutations():
    assert "export function scheduleLibraryRefresh" in REFRESH
    assert "'make_library_file'" in REFRESH
    assert "'manage_library_folders'" in REFRESH
    assert "'create_document'" in REFRESH
    assert "'manage_documents'" in REFRESH
    assert "MUTATING_DOC_ACTIONS = new Set(['move', 'delete', 'tidy'])" in REFRESH
    assert "JSON.parse(json.command" in REFRESH


def test_refresh_helper_skips_approval_placeholders_and_failures():
    assert "json.ask_user" in REFRESH
    assert "json.exit_code === 0 || json.exit_code == null" in REFRESH
    assert "REFRESH_DEBOUNCE_MS = 600" in REFRESH
    assert "new CustomEvent('library-refresh')" in REFRESH


def test_document_library_listens_and_refetches_tree_and_grid():
    assert "window.addEventListener('library-refresh'" in LIB
    assert "if (!_libraryOpen || !document.getElementById('doclib-modal')) return;" in LIB
    assert "Promise.all([folderFetch(), libraryFetch(false)])" in LIB


def test_all_agent_surfaces_schedule_the_refresh():
    for source, import_path in (
        (CHAT, "./libraryRefresh.js"),
        (COMPARE, "../libraryRefresh.js"),
        (GROUP, "./libraryRefresh.js"),
    ):
        assert (
            f"import {{ scheduleLibraryRefresh }} from '{import_path}';"
            in source
        )
        assert "scheduleLibraryRefresh(json);" in source
