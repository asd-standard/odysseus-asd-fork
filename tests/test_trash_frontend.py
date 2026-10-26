"""Source-level guards for the Library Trash frontend wiring.

documentLibrary.js is a browser module with DOM-only imports, so these assert
the user-visible contract at the source level (same approach as
test_document_library_delete_counters.py).
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LIB = (ROOT / "static" / "js" / "documentLibrary.js").read_text(encoding="utf-8")
DOC = (ROOT / "static" / "js" / "document.js").read_text(encoding="utf-8")
SETTINGS = (ROOT / "src" / "settings.py").read_text(encoding="utf-8")


def test_trash_tab_and_panel_are_wired():
    assert 'data-doclib-tab="trash"' in LIB
    assert 'data-doclib-panel="trash"' in LIB
    assert 'id="doclib-trash-grid"' in LIB
    assert "_renderLibTrash()" in LIB
    assert "else if (tab === 'trash') _renderLibTrash();" in LIB


def test_trash_reads_documents_and_research():
    assert "/api/documents/trash" in LIB
    assert "/api/research/trash" in LIB
    assert "retention_days" in LIB
    assert "days_left" in LIB


def test_trash_actions_use_restore_and_permanent_routes():
    assert "/restore`" in LIB
    assert "/permanent`" in LIB
    assert "/api/documents/trash/empty" in LIB
    assert "/api/research/trash/empty" in LIB


def test_library_deletes_describe_moving_to_trash():
    assert 'Move "${name}" to Trash?' in LIB
    assert "Moved to Trash" in LIB
    assert "Move ${count} document" in LIB
    assert 'Move ${count} research report' in LIB


def test_internal_scratch_document_cleanup_is_permanent():
    # Post-send compose docs, empty-doc auto-delete, detach, and discard must
    # not fill Trash. The user-facing tab delete still uses the normal route.
    assert DOC.count("/permanent`, { method: 'DELETE' }") >= 4
    assert 'Move "${name}" to Trash?' in DOC
    assert "Moved to Trash" in DOC


def test_trash_retention_defaults_to_15_days():
    assert '"trash_retention_days": 15' in SETTINGS
