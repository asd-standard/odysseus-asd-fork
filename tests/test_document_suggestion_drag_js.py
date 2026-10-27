"""The suggestion card can be dragged and stays where it is dropped.

The card is an auto-anchored, `position: fixed` panel: `_positionCard()` recomputes
`top`/`left` from the commented line on every render, scroll and resize. Making it
draggable therefore needs three things that are easy to get wrong:

  * the drag position must live OUTSIDE the element, because
    `_showCurrentSuggestion()` destroys and rebuilds the card for every
    Accept/Skip/‹ › step;
  * auto-anchoring must stand down once the user has placed the card, while the
    text highlight keeps following scroll;
  * the stored position must be clamped on every application, or a resize
    strands the card off-screen.

Source-structure assertions: document.js is browser-coupled and not importable
in pytest (same approach as the sibling `*_js.py` suites).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "static/js/document.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


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


SHOW = _function_from(DOC, "function _showCurrentSuggestion(")
MAKE = _function_from(DOC, "function _makeSuggCardDraggable(card)")
APPLY = _function_from(DOC, "function _applySuggCardPos(card, left, top)")
DETACH_RESET = "let _suggCardPos = null;"


# ── state lives outside the card ───────────────────────────────────────────

def test_drag_position_is_module_state_not_element_state():
    assert DETACH_RESET in DOC
    # The card is rebuilt per suggestion, so the position must be re-applied on
    # every build — not left on the discarded element.
    assert "_suggCardPos" in SHOW
    assert "if (_suggCardPos)" in SHOW


def test_rebuilt_card_applies_the_stored_position_without_flashing():
    # Applied synchronously (before the rAF anchor pass) so stepping through
    # suggestions never shows the card at the anchor first.
    idx_apply = SHOW.index("_applySuggCardPos(card, _suggCardPos.left")
    idx_raf = SHOW.index("requestAnimationFrame(() => { _positionCard(card)")
    assert idx_apply < idx_raf


# ── anchoring stands down, highlight keeps tracking ────────────────────────

def test_position_card_defers_to_a_dragged_position():
    assert "if (_suggCardPos) {" in SHOW
    # ...and returns before the anchor math runs.
    guard = SHOW.index("if (_suggCardPos) {")
    anchor_math = SHOW.index("const linesBefore")
    assert guard < anchor_math


def test_highlight_refreshes_independently_of_card_placement():
    assert "_refreshSuggHighlight" in SHOW
    assert "_highlightSuggestionText(sugg.find)" in SHOW
    reposition = SHOW[SHOW.index("const _reposition = ()"):]
    reposition = reposition[: reposition.index("};")]
    assert "_refreshSuggHighlight()" in reposition


# ── drag mechanics ─────────────────────────────────────────────────────────

def test_header_is_the_drag_handle_and_buttons_are_excluded():
    assert "'.doc-suggestion-header'" in MAKE
    assert "e.target.closest('button')" in MAKE
    for evt in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
        assert evt in MAKE


def test_drag_uses_pointer_capture_so_fast_moves_are_not_lost():
    assert "setPointerCapture" in MAKE
    assert "releasePointerCapture" in MAKE


def test_double_click_reanchors_to_the_text():
    assert "dblclick" in MAKE
    assert "_suggCardPos = null;" in MAKE


# ── clamping ───────────────────────────────────────────────────────────────

def test_stored_position_is_clamped_into_the_viewport():
    assert "MIN_VISIBLE" in APPLY
    assert "window.innerWidth" in APPLY and "window.innerHeight" in APPLY
    # Clamped on both axes, and the clamped value is what gets stored.
    assert "Math.min(Math.max(" in APPLY
    assert "_suggCardPos = _applySuggCardPos" in MAKE


def test_every_position_application_keeps_the_clamped_value():
    # Resize path re-clamps instead of trusting the stored numbers.
    assert "_suggCardPos = _applySuggCardPos(card, _suggCardPos.left, _suggCardPos.top)" in SHOW


# ── resets ─────────────────────────────────────────────────────────────────

def test_position_resets_when_the_queue_drains():
    drain = SHOW[: SHOW.index("_exitSuggestionPreview()")]
    assert "_suggCardPos = null;" in drain


def test_position_resets_on_clear_all():
    clear = _function_from(DOC, "function clearAllSuggestions(")
    assert "_suggCardPos = null;" in clear


def test_position_resets_when_switching_documents():
    switch = _function_from(DOC, "function switchToDoc(")
    assert "_suggCardPos = null;" in switch


# ── affordance ─────────────────────────────────────────────────────────────

def test_header_shows_a_grab_cursor_and_is_touch_friendly():
    header = CSS[CSS.index(".doc-suggestion-header {"):]
    header = header[: header.index("}")]
    assert "cursor: grab" in header
    assert "touch-action: none" in header
    assert "user-select: none" in header


def test_dragging_state_switches_to_grabbing_cursor():
    assert ".doc-suggestion-card.dragging .doc-suggestion-header" in CSS
    assert "grabbing" in CSS


def test_drag_class_is_toggled_during_the_gesture():
    assert "classList.add('dragging')" in MAKE
    assert "classList.remove('dragging')" in MAKE
