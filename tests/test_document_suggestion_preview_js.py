"""Suggestion diff preview — real editor content, read-only while pending.

The previous implementation injected a colour overlay into the syntax-highlight
layer, which lives *behind* a `color: transparent` textarea: deleted text showed
as an empty smeared band, inserted text showed characters that were not in the
document, and multi-line suggestions collapsed into one unreadable line. This
suite locks the replacement behaviour:

  * the diff is rendered as REAL text (a read-only <pre> over the textarea);
  * the editor is read-only from the first suggestion until the queue drains;
  * the textarea is never written with preview content, so nothing can save it;
  * every exit path restores the pristine text.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_JS = (ROOT / "static/js/document.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


def _function_from(src: str, signature: str) -> str:
    """Brace-match a function body from its signature (see the sibling _js tests)."""
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


SHOW_CURRENT = _function_from(DOC_JS, "function _showCurrentSuggestion(")
RENDER = _function_from(DOC_JS, "function _renderSuggestionPreview(")
EXIT = _function_from(DOC_JS, "function _exitSuggestionPreview(")


# --------------------------------------------------------------------------
# The overlay implementation is gone
# --------------------------------------------------------------------------


def test_colour_overlay_diff_is_removed():
    # It rendered into the transparent-textarea highlight layer and was
    # unreadable; it must not come back.
    assert "function _showInlineDiff(" not in DOC_JS
    assert "function _clearInlineDiff(" not in DOC_JS
    assert "sugg-inline-del" not in DOC_JS
    assert ".sugg-inline-del" not in CSS
    assert ".sugg-inline-add" not in CSS


# --------------------------------------------------------------------------
# Rendered as real content
# --------------------------------------------------------------------------


def test_suggestion_renders_a_real_text_preview():
    assert "_renderSuggestionPreview(sugg)" in SHOW_CURRENT
    assert "doc-suggestion-preview" in RENDER
    # Real rows with visible marker glyphs, not spans in the highlight layer.
    assert "sugg-line-del" in RENDER
    assert "sugg-line-add" in RENDER
    assert "'−' + e.line" in RENDER
    assert "'+' + e.line" in RENDER


def test_preview_uses_the_line_level_diff():
    # One diff implementation in the file; no third algorithm.
    assert "_computeLineDiff(regionOld, newRegion)" in RENDER


def test_preview_layer_matches_editor_typography():
    # Mis-matched metrics make the gutter and the diff drift apart.
    block = CSS[CSS.index(".doc-suggestion-preview {"):]
    block = block[: block.index("}")]
    assert "padding: 10px 12px 10px 48px" in block
    assert "line-height: 1.45 !important" in block
    assert "font-size: 11px !important" in block


# --------------------------------------------------------------------------
# Read-only while pending
# --------------------------------------------------------------------------


def test_editor_becomes_read_only_on_first_suggestion():
    assert "textarea.readOnly = true" in RENDER
    # Only on entry, not on every re-render.
    assert "if (!_suggestionPreviewActive) {" in RENDER


def test_editor_becomes_editable_again_on_exit():
    assert "textarea.readOnly = false" in EXIT


def test_editor_is_restored_from_the_captured_copy_not_the_textarea():
    # The pristine text is captured on ENTRY from the document model...
    assert "docs.get(docId).content" in RENDER
    assert "_suggestionPreviewOriginal =" in RENDER
    # ...and the restore never trusts what is currently on screen.
    assert "textarea.value = original" in EXIT
    assert "_suggestionPreviewOriginal = null" in EXIT


def test_preview_never_writes_the_document_textarea():
    # The preview lives in its own <pre>; the textarea is only hidden. Writing
    # diff text into it would let autosave/saveCurrentToMap persist the preview.
    assert "textarea.value =" not in RENDER
    assert "doc-suggestion-previewing" in RENDER


# --------------------------------------------------------------------------
# Every exit path restores
# --------------------------------------------------------------------------


def test_exit_runs_when_the_queue_drains():
    assert "if (_activeSuggestions.length === 0)" in SHOW_CURRENT
    assert "_exitSuggestionPreview()" in SHOW_CURRENT


def test_exit_runs_on_clear_all():
    clear_all = _function_from(DOC_JS, "function clearAllSuggestions(")
    assert "_exitSuggestionPreview()" in clear_all


def test_exit_runs_before_switching_documents():
    switch = _function_from(DOC_JS, "function switchToDoc(")
    assert "_exitSuggestionPreview()" in switch
    # ...and before the editor is repopulated with the next document, or the
    # preview leaks across.
    body_after = switch[switch.index("_exitSuggestionPreview()"):]
    assert "textarea.value = doc.content" in body_after


def test_exit_runs_before_closing_the_document():
    close = _function_from(DOC_JS, "function _closeWithoutDeleting(")
    assert "_exitSuggestionPreview()" in close
    assert close.index("_exitSuggestionPreview()") < close.index("saveDocument(")


# --------------------------------------------------------------------------
# Accept composes on the pristine text and saves once
# --------------------------------------------------------------------------


def test_accept_edits_the_pristine_text_not_the_hidden_textarea():
    apply_fn = _function_from(DOC_JS, "function _applySuggestion(")
    assert "_suggestionPreviewOriginal" in apply_fn
    assert "_suggestionPreviewOriginal = updated" in apply_fn


def test_accept_does_not_save_per_suggestion():
    apply_fn = _function_from(DOC_JS, "function _applySuggestion(")
    assert "saveDocument(" not in apply_fn
    animate = _function_from(DOC_JS, "function _animateNext(")
    # One save when the queue drains, not one per card.
    assert "saveDocument({ silent: true })" in animate
    assert "_activeSuggestions.length === 0" in animate


def test_suggestion_card_height_is_bounded_and_reason_scrolls():
    """A long model explanation must not push Accept/Skip off-screen.

    The card is a flex column with a max-height; the header and the action row
    are non-shrinking, and only the reason scrolls. Without this the card grew
    past the viewport and the buttons became unreachable.
    """
    card = CSS[CSS.index(".doc-suggestion-card {"):]
    card = card[: card.index("}")]
    assert "max-height: min(70vh, 460px)" in card
    assert "display: flex" in card
    assert "flex-direction: column" in card
    # overflow stays visible so the ::before arrow can stick out; the inner
    # reason element is what scrolls.
    assert "overflow: visible" in card

    header = CSS[CSS.index(".doc-suggestion-header {"):]
    header = header[: header.index("}")]
    assert "flex: 0 0 auto" in header

    reason = CSS[CSS.index(".doc-suggestion-reason {"):]
    reason = reason[: reason.index("}")]
    assert "overflow-y: auto" in reason
    assert "flex: 1 1 auto" in reason
    assert "min-height" in reason
    assert "overflow-wrap: anywhere" in reason

    actions = CSS[CSS.index(".doc-suggestion-actions {"):]
    actions = actions[: actions.index("}")]
    assert "flex: 0 0 auto" in actions


def test_card_vertical_clamp_uses_real_height():
    """_positionCard must clamp against offsetHeight, not a fixed guess.

    The old `innerHeight - 220` assumed a short card; with a long explanation
    the buttons ended up below the fold.
    """
    position = _function_from(DOC_JS, "function _positionCard(")
    assert "card.offsetHeight" in position
    assert "window.innerHeight - cardH" in position
    assert "window.innerHeight - 220" not in position
    # Horizontal side-gap must also follow the real width (card is 320px now).
    assert "card.offsetWidth" in position
    assert "paneRect.right + 270" not in position


def test_card_width_and_mobile_gutter():
    card = CSS[CSS.index(".doc-suggestion-card {"):]
    card = card[: card.index("}")]
    assert "width: 320px" in card
    assert "max-width: calc(100vw - 24px)" in card

    position = _function_from(DOC_JS, "function _positionCard(")
    assert "card.style.left = '8px'" in position
    assert "card.style.right = '8px'" in position


# --------------------------------------------------------------------------
# The abandoned second apply path must stay gone
# --------------------------------------------------------------------------


def test_legacy_suggestion_helpers_are_removed():
    """`acceptSuggestion(id)`/`dismissSuggestion(id)` were orphans.

    Nothing called them, and they still applied edits by writing straight into
    `textarea.value` + saving per suggestion — the exact mechanism the
    read-only preview replaced. If they come back, so does the silent
    write-then-save path alongside the preview.
    """
    assert "function acceptSuggestion(" not in DOC_JS
    assert "function dismissSuggestion(" not in DOC_JS
    # Only the explanatory comment may mention them.
    for name in ("acceptSuggestion", "dismissSuggestion"):
        for line in DOC_JS.splitlines():
            if name in line:
                assert line.strip().startswith("*"), (
                    f"{name} is referenced outside the explanatory comment: {line!r}"
                )


def test_dead_suggestions_container_is_gone():
    # The element never existed in the DOM; the lookups were always null.
    assert "doc-suggestions-container" not in DOC_JS.replace("#doc-suggestions-container", "")


def test_unused_handler_alias_is_gone():
    assert "_origHandleDocSuggestions" not in DOC_JS

