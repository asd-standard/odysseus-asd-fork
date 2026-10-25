"""Library preview must not open empty documents or delete real ones.

Two independent regressions lived in the preview panel:

1. `libraryShowPreview()` registered the clicked row in the editor's tab map.
   A library-list row carries only a 500-char `preview` field — no
   `current_content` — so the tab entry had `content: ''`. Then:
     * `loadDocument()` took its "already loaded" fast path and opened an EMPTY
       editor, and
     * `_detachDocFromSession()` saw empty local content on close and issued a
       DELETE, so the file vanished from the Library (soft delete — content was
       still on the server).

2. The preview's ⋮ menu rendered the shared `_docActionItems()` model, whose
   entries expose `run`, but `_showLibDropdown()` called `item.action()`.
   Calling undefined threw inside the click handler, which is why every menu
   entry did nothing.

These are source-structure assertions: document.js / documentLibrary.js are
browser-coupled and not importable in pytest (same approach as the sibling
`*_js.py` suites).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = (ROOT / "static/js/documentLibrary.js").read_text(encoding="utf-8")
DOC = (ROOT / "static/js/document.js").read_text(encoding="utf-8")


def _function_from(src: str, signature: str) -> str:
    start = src.index(signature)
    i = src.index("{\n", start)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces after {signature!r}")


SHOW_PREVIEW = _function_from(LIB, "function libraryShowPreview(")
OPEN_DOC = _function_from(LIB, "function _showLibDropdown(")
ADD_TO_TABS = _function_from(DOC, "function addDocToTabs(")
LOAD_DOC = _function_from(DOC, "export async function loadDocument(")
DETACH = _function_from(DOC, "function _detachDocFromSession(")
SWITCH = _function_from(DOC, "function switchToDoc(")


# ── 1. the preview never seeds the tab map ─────────────────────────────────

def test_preview_does_not_register_the_document_in_the_tab_map():
    """This registration is what created an empty, deletable tab entry."""
    assert "_addDocToTabs" not in SHOW_PREVIEW
    # The rule is documented in place so it cannot be "helpfully" restored.
    assert "DO NOT register the previewed doc" in SHOW_PREVIEW


def test_preview_keeps_only_its_own_dataset_id():
    # getDisplayedDocId()/getLibraryPreviewDocId() read this, so the preview
    # stays addressable without a tab entry.
    assert "panel.dataset.docId = doc.id" in SHOW_PREVIEW


# ── 2. hydration gate ──────────────────────────────────────────────────────

def test_add_doc_to_tabs_records_whether_content_was_present():
    assert "hydrated" in ADD_TO_TABS
    assert "doc.current_content !== undefined || doc.content !== undefined" in ADD_TO_TABS


def test_load_document_fetches_unhydrated_entries():
    # Fast path must require hydration; otherwise a `preview`-only row opens
    # blank instead of fetching the real text.
    assert "cached = docs.get(docId)" in LOAD_DOC
    assert "cached.hydrated !== false" in LOAD_DOC
    assert "fetch(`${API_BASE}/api/document/${docId}`)" in LOAD_DOC


# ── 3. never delete on "local content empty" ───────────────────────────────

def test_detach_only_discards_documents_that_were_never_saved():
    assert "_unsaved === true" in DETACH
    # The old blanket `else fetch(... DELETE ...)` is what destroyed files.
    assert "} else {\n      fetch(" not in DETACH


def test_switching_away_only_deletes_unsaved_empty_docs():
    assert "prev._unsaved === true" in SWITCH


def test_new_documents_are_marked_unsaved_and_cleared_on_save():
    assert "_created._unsaved = true" in DOC
    assert "_saved._unsaved = false" in DOC


# ── 4. the shared menu model must dispatch ─────────────────────────────────

def test_dropdown_supports_both_menu_shapes():
    """`_docActionItems` uses `run`; folder/chat menus use `action`."""
    assert "item.run || item.action" in OPEN_DOC
    assert "item.action();" not in OPEN_DOC


def test_dropdown_propagates_item_title():
    assert "item.title" in OPEN_DOC


def test_card_menu_still_uses_run():
    # The card renderer always dispatched `run`; keep it that way.
    assert "item.run();" in LIB


# ── 5. the preview button reuses the canonical open path ───────────────────

def test_preview_open_button_uses_library_open_document():
    assert "await libraryOpenDocument(doc)" in SHOW_PREVIEW
    # No hand-rolled load that could diverge from the menu path.
    assert "_loadDocument(doc.id)" not in SHOW_PREVIEW
