"""The editor must keep reporting the document the user is actually looking at.

Regression guard for the "the LLM says no document is open" failure: a document
opened from the Library (or otherwise detached, ``session_id = NULL``) is not in
``/api/documents/<session>``'s ``activeDocs`` list, so ``loadSessionDocs`` used to
null ``activeDocId`` on every session switch. The panel stayed on screen while
``getCurrentDocId()`` returned null, so chat sent no ``active_doc_id`` and the
agent was told "no document is open".

``static/js/document.js`` is browser-coupled and not importable in pytest, so
these are source-structure checks over the real function bodies.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_JS = (ROOT / "static/js/document.js").read_text()
CHAT_JS = (ROOT / "static/js/chat.js").read_text()

def _function_from(src: str, signature: str) -> str:
    """Brace-match a function body, starting at the first `{` after its params.

    `_function_body` scans from the signature and stops at the `{}` in a default
    parameter; these helpers therefore match from the opening body brace.
    """
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


LOAD_SESSION_DOCS = _function_from(DOC_JS, "export async function loadSessionDocs(")
GET_DISPLAYED = _function_from(DOC_JS, "export function getDisplayedDocId()")


def test_load_session_docs_does_not_unconditionally_clear_active_doc():
    # The old bug: `activeDocId = null;` right after pruning other sessions'
    # tabs, before the fetch. A detached doc was then never restored.
    assert "activeDocId = null;\n\n    // Show loading state" not in LOAD_SESSION_DOCS
    assert "_keepPreviousActive" in LOAD_SESSION_DOCS


def test_load_session_docs_keeps_active_doc_when_session_has_none():
    # No active docs for this session, but the user still has one open: it must
    # survive (and must not fall through to the empty-state early return).
    body = LOAD_SESSION_DOCS
    empty_branch = body[body.index("if (activeDocs.length === 0)") :]
    empty_branch = empty_branch[: empty_branch.index("for (const doc of activeDocs)")]
    assert "_keepPreviousActive" in empty_branch
    assert "return;" in empty_branch


def test_load_session_docs_does_not_override_a_document_being_viewed():
    # Both the restore/minimize path and the normal path must bail out before
    # switchToDoc(target.id) when the user already has a document open.
    keep_at = LOAD_SESSION_DOCS.rindex("if (_keepPreviousActive) {")
    last_switch = LOAD_SESSION_DOCS.rindex("switchToDoc(target.id);")
    assert keep_at < last_switch


def test_keep_previous_active_does_not_require_the_tab_map():
    # A detached document (session_id NULL) is not returned by
    # /api/documents/<session>, and the tab map is pruned on every load. Keying
    # the guard on docs.has() dropped exactly that document.
    assert "_keepPreviousActive =\n      !!_prevActiveDocId && docs.has(_prevActiveDocId)" not in LOAD_SESSION_DOCS
    assert "const _keepPreviousActive = !!_prevActiveDocId && _paneShowing;" in LOAD_SESSION_DOCS


def test_restoring_a_displayed_document_keeps_the_pointer():
    restore = _function_from(DOC_JS, "async function _restoreDisplayedDoc(")
    assert "activeDocId = docId;" in restore
    assert "_publishActiveDoc(docId)" in restore
    # It must re-fetch the doc into the tab map rather than assume it is there.
    assert "/api/document/" in restore


def test_get_displayed_doc_id_prefers_editor_then_library_preview():
    # The honest "what is on screen" answer — never a DB guess.
    assert "if (activeDocId) return activeDocId;" in GET_DISPLAYED
    assert "getLibraryPreviewDocId()" in GET_DISPLAYED
    # ...and the preview fallback itself still reads the preview panel.
    preview = _function_from(DOC_JS, "export function getLibraryPreviewDocId()")
    assert ".doclib-preview-panel" in preview


def test_active_doc_pointer_does_not_depend_on_the_tab_map():
    # Requiring docs.has(activeDocId) made a real pointer vanish whenever the
    # tab map was cleared, producing "editor open but nothing registered".
    assert "docs.has(activeDocId)" not in GET_DISPLAYED
    assert "if (activeDocId) return activeDocId;" in GET_DISPLAYED


def test_is_showing_document_requires_a_nameable_document():
    showing = _function_from(DOC_JS, "export function isShowingDocument()")
    # A mounted pane alone must not count as "a document is open".
    assert "doc-editor-pane" not in showing
    stale = _function_from(DOC_JS, "export function hasStaleEditorPane()")
    assert "doc-editor-pane" in stale


def test_switch_to_doc_loads_instead_of_silently_returning():
    switch = _function_from(DOC_JS, "function switchToDoc(")
    assert "if (!docs.has(docId)) {" in switch
    assert "loadDocument(docId)" in switch


def test_clear_all_does_not_leave_a_phantom_pane():
    clear = _function_from(DOC_JS, "export function clearAll()")
    # Blanking the editor keeps "pane visible" and "document registered" in sync.
    assert "doc-editor-textarea" in clear


def test_library_preview_offers_open_in_editor():
    lib = (ROOT / "static/js/documentLibrary.js").read_text()
    preview = _function_from(lib, "function libraryShowPreview(")
    assert "doclib-preview-open" in preview
    # Reuses the canonical open path (same as the ⋮ "Open" entry) rather than a
    # hand-rolled load that could diverge from it.
    assert "libraryOpenDocument(doc)" in preview
    # The preview must NOT seed the tab map: a library-list row has no
    # `current_content`, so doing so opened an empty editor and let the close
    # path soft-delete the real file. See test_library_preview_safety_js.py.
    assert "_addDocToTabs" not in preview


def test_chat_sends_editor_signals_when_present():
    # The backend needs to distinguish "nothing open" from "open but not the
    # chat's active target"; otherwise it reports the wrong reason to the agent.
    assert "fd.append('displayed_doc_id'" in CHAT_JS
    assert "fd.append('editor_open'" in CHAT_JS
    assert "fd.append('library_preview_doc_id'" in CHAT_JS
    assert "fd.append('editor_stale'" in CHAT_JS
    assert "fd.append('document_selection'" in CHAT_JS


def test_chat_no_longer_forces_agent_mode_for_open_documents():
    # Whether to act on the visible document is the model's decision.
    assert "if (!isIncognito && !isAgentMode && documentModule && activeDocIdForSend)" not in CHAT_JS
