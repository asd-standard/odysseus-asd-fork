"""One window in front: opening a modal collapses the one below to a chip.

Behaviour: opening or restoring a tool window collapses whatever was in front of
it, and when that window leaves the foreground the displaced one takes its place
back. The sequencing lives in `static/js/foregroundStack.js`, which is deliberately
DOM-free — these tests run the REAL module under Node (extracted from source, so
there is no duplicated implementation to drift).

The wiring is asserted structurally: modalManager attaches the visibility
observers, routes hides through the stack, and the document panel (which has no
modal element of its own) reports its own open/close.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK_JS = (ROOT / "static/js/foregroundStack.js").read_text(encoding="utf-8")
MM = (ROOT / "static/js/modalManager.js").read_text(encoding="utf-8")
DOC = (ROOT / "static/js/document.js").read_text(encoding="utf-8")
LIB = (ROOT / "static/js/documentLibrary.js").read_text(encoding="utf-8")
NOTES = (ROOT / "static/js/notes.js").read_text(encoding="utf-8")
TASKS = (ROOT / "static/js/tasks.js").read_text(encoding="utf-8")
EMAIL = (ROOT / "static/js/emailLibrary.js").read_text(encoding="utf-8")
RESEARCH = (ROOT / "static/js/research/panel.js").read_text(encoding="utf-8")
COMPARE = (ROOT / "static/js/compare/index.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


# ── behavioural: run the real state machine ────────────────────────────────

def _run(ops):
    """Execute the stack module under node with a scripted op sequence.

    ops: [["open", id], ["left", id], ["suppress", id], ["forget", id]]
    -> {"out": [...], "fg": [...]}
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    src = STACK_JS.replace(
        "export function createForegroundStack", "function createForegroundStack"
    )
    script = (
        src
        + "\nconst s = createForegroundStack();\nconst ops = " + json.dumps(ops) + ";\n"
        + "const out = [];\n"
        + "for (const [op, id] of ops) {\n"
        + "  if (op === 'open') out.push(['open', id, s.opened(id)]);\n"
        + "  else if (op === 'left') out.push(['left', id, s.left(id)]);\n"
        + "  else if (op === 'suppress') { s.suppressNext(id); out.push(['suppress', id]); }\n"
        + "  else if (op === 'forget') { s.forget(id); out.push(['forget', id]); }\n"
        + "}\n"
        + "process.stdout.write(JSON.stringify({ out, fg: s.foreground() }));"
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _minimized(result):
    """Ids the stack asked to collapse, in order."""
    return [r[2]["minimize"] for r in result["out"] if r[0] == "open" and r[2].get("minimize")]


def _restored(result):
    return [r[2]["restore"] for r in result["out"] if r[0] == "left" and r[2].get("restore")]


def test_opening_a_second_window_collapses_the_first():
    r = _run([["open", "editor"], ["open", "library"]])
    assert _minimized(r) == ["editor"]
    assert r["fg"] == ["library"]


def test_closing_the_replacement_restores_the_displaced_window():
    r = _run([
        ["open", "editor"],
        ["open", "library"], ["suppress", "editor"], ["left", "editor"],
        ["left", "library"],
    ])
    assert _restored(r) == ["editor"]


def test_three_levels_unwind_in_order():
    """editor → library → settings, then close back down the chain."""
    r = _run([
        ["open", "editor"],
        ["open", "library"], ["suppress", "editor"], ["left", "editor"],
        ["open", "settings"], ["suppress", "library"], ["left", "library"],
        # close settings → library comes back
        ["left", "settings"],
        ["open", "library"],
        # close library → editor comes back
        ["left", "library"],
    ])
    assert _minimized(r) == ["editor", "library"]
    assert _restored(r) == ["library", "editor"]


def test_programmatic_minimise_does_not_unwind_the_grandparent():
    """Suppression: collapsing editor because library opened must not also
    restore whatever the editor had displaced."""
    r = _run([
        ["open", "notes"],
        ["open", "editor"], ["suppress", "notes"], ["left", "notes"],
        ["open", "library"], ["suppress", "editor"], ["left", "editor"],
    ])
    # The suppressed left() of editor must not hand the screen to notes.
    assert _restored(r) == []
    assert r["fg"] == ["library"]


def test_user_minimising_the_front_window_restores_the_one_below():
    r = _run([
        ["open", "editor"],
        ["open", "library"], ["suppress", "editor"], ["left", "editor"],
        # user minimizes library themselves (no suppression)
        ["left", "library"],
    ])
    assert _restored(r) == ["editor"]


def test_closing_a_chip_drops_the_chain_instead_of_resurrecting():
    """A minimized window closed from its chip must not bring anything back."""
    r = _run([
        ["open", "editor"],
        ["open", "library"], ["suppress", "editor"], ["left", "editor"],
        ["forget", "editor"],
        ["left", "library"],
    ])
    assert _restored(r) == []


def test_forget_removes_pairs_referencing_a_destroyed_window():
    r = _run([
        ["open", "editor"],
        ["open", "library"], ["suppress", "editor"], ["left", "editor"],
        ["forget", "library"],
        ["left", "library"],
    ])
    assert r["fg"] == []
    assert _restored(r) == []


def test_reopening_the_same_window_is_idempotent():
    r = _run([["open", "editor"], ["open", "editor"]])
    assert _minimized(r) == []
    assert r["fg"] == ["editor"]


def test_restoring_a_chip_puts_it_back_in_front_and_collapses_the_other():
    r = _run([
        ["open", "editor"],
        ["open", "library"], ["suppress", "editor"], ["left", "editor"],
        # user clicks the editor chip while library is open
        ["open", "editor"],
        ["suppress", "library"], ["left", "library"],
        # closing the editor restores the library it displaced
        ["left", "editor"],
    ])
    assert _minimized(r) == ["editor", "library"]
    assert _restored(r) == ["library"]


# ── wiring: modalManager ───────────────────────────────────────────────────

def test_manager_imports_and_uses_the_stack():
    assert "import { createForegroundStack } from './foregroundStack.js';" in MM
    assert "createForegroundStack()" in MM
    assert "function _fgOpened(" in MM
    assert "function _fgLeft(" in MM


def test_visibility_transitions_feed_the_stack():
    # Both directions must be wired, or a close could never restore anything.
    assert "_fgOpened(id);" in MM
    assert "_fgLeft(id);" in MM
    # The observer is attached centrally, so lazily built windows are covered.
    assert "function _observeModal(" in MM
    assert "_observeModal(id);" in MM


def test_lazily_built_modals_are_observed_when_inserted():
    # The Library is created on first open; without this it could not displace.
    assert "MutationObserver((muts) =>" in MM
    assert "_observeAllModals(node)" in MM
    assert "[data-window-id]" in MM


def test_displacement_suppresses_the_collapse_transition():
    # Otherwise collapsing editor would also restore what IT displaced.
    collapse = MM[MM.index("function _collapseForDisplacement("):]
    collapse = collapse[: collapse.index("\n}")]
    assert "_fgStack.suppressNext(id)" in collapse


def test_closing_a_chip_forgets_but_a_foreground_close_does_not():
    close_fn = MM[MM.index("export function close(id) {"):]
    close_fn = close_fn[: close_fn.index("\n}")]
    assert "if (s.isMinimized) _fgStack.forget(id);" in close_fn


def test_unregister_forgets_after_pending_observer_callbacks():
    unreg = MM[MM.index("export function unregister(id) {"):]
    unreg = unreg[: unreg.index("\n}")]
    assert "setTimeout(() => _fgStack.forget(id), 0)" in unreg


def test_reference_overlay_can_opt_out():
    can_displace = MM[MM.index("function _canDisplace(id) {"):]
    can_displace = can_displace[: can_displace.index("\n}")]
    assert "data-mm-no-displace" in can_displace
    assert "_NO_DISPLACE" not in MM


# ── wiring: the document panel (virtual window) ────────────────────────────

def test_open_panel_takes_the_foreground():
    open_fn = DOC[DOC.index("export function openPanel("):]
    open_fn = open_fn[: open_fn.index("\n  }")]
    assert "Modals.notifyOpened('doc-panel')" in open_fn


def test_close_panel_hands_the_place_back():
    close_fn = DOC[DOC.index("export function closePanel("):]
    close_fn = close_fn[: close_fn.index("\n  }")]
    assert "Modals.notifyLeft('doc-panel')" in close_fn
    # A real close is terminal: nothing may restore it later.
    assert "Modals.forgetForeground('doc-panel')" in close_fn


def test_panel_registers_its_own_collapse_hook():
    # Modals.minimize() cannot hide a panel that has no modal element.
    assert "registerMinimizeHook('doc-panel'" in DOC
    assert "Modals.notifyOpened(" in DOC and "Modals.notifyLeft(" in DOC


def test_library_close_reports_itself():
    """The Library element is REMOVED, not hidden, so no visibility event fires."""
    close_fn = LIB[LIB.index("export function closeLibrary("):]
    close_fn = close_fn[: close_fn.index("\n  }")]
    assert "Modals.notifyClosed('doclib-modal', { destroyed: true })" in close_fn


def test_manager_reports_destroyed_windows():
    fn = MM[MM.index("export function notifyClosed("):]
    fn = fn[: fn.index("\n}")]
    assert "_fgLeft(id)" in fn
    assert "_fgStack.forget(id)" in fn
    assert "unregister(id)" in fn


def test_close_reports_when_close_fn_removed_the_element():
    close_fn = MM[MM.index("export function close(id) {"):]
    close_fn = close_fn[: close_fn.index("\n/** Inject a minimize")]
    assert "if (!s || _closing.has(id)) return;" in close_fn
    assert "notifyClosed(id, { destroyed: true })" in close_fn


def test_remove_based_windows_report_their_close():
    """Every window that removes its element must report the departure itself;
    the visibility observer only sees class/style changes."""
    for source, wid in (
        (TASKS, "tasks-modal"),
        (EMAIL, "email-lib-modal"),
        (RESEARCH, "research-overlay"),
        (COMPARE, "compare-model-overlay"),
        (LIB, "doclib-modal"),
    ):
        assert f"notifyClosed('{wid}', {{ destroyed: true }})" in source


def test_dismiss_path_reports_closed():
    fn = APP_JS[APP_JS.index("function dismissModal(modal) {"):]
    fn = fn[: fn.index("\n  }")]
    assert "Modals.notifyClosed(modal.id" in fn
    assert "destroyed: _isDynamicWindow" in fn


def test_edge_docked_windows_are_not_collapsed():
    """A left/right docked window is layout, not foreground: opening a tool
    over it must not turn it into a chip."""
    body = MM[MM.index("function _fgOpened("):]
    body = body[: body.index("\n}")]
    assert "_isEdgeDocked(_windowEl(parent))" in body
    helper = MM[MM.index("function _isEdgeDocked("):]
    helper = helper[: helper.index("\n}")]
    assert "modal-left-docked" in helper
    assert "modal-right-docked" in helper


# ── the collapsed window becomes a chip above the composer ─────────────────

def test_displaced_window_chip_parks_bottom_left_above_the_composer():
    assert "function _seedChipPosition(" in MM
    seed = MM[MM.index("function _seedChipPosition("):]
    seed = seed[: seed.index("\n}")]
    assert "left: 12" in seed
    assert "--composer-clearance" in MM
    # Stacking upwards keeps several collapsed windows from overlapping.
    assert "taken * 40" in seed
    # Only when the user has not placed it: their position wins.
    assert "_chipPositions.has(id)" in seed


def test_chip_is_seeded_for_both_collapse_paths():
    collapse = MM[MM.index("function _collapseForDisplacement("):]
    collapse = collapse[: collapse.index("\n}")]
    assert collapse.count("_seedChipPosition(id)") == 2   # hook path + minimize path


def test_free_chips_render_on_desktop():
    # The chip is only free-positioned when displaced/dragged, so removing the
    # mobile-only gate does not change docked chips.
    render = MM[MM.index("function _renderDock("):]
    render = render[: render.index("\n}")]
    assert "if (pos) {" in render
    assert "pos && window.innerWidth <= 768" not in render


def test_desktop_drag_of_a_positioned_chip_is_a_free_move():
    down = MM[MM.index("const onPointerDown = (e) => {"):]
    down = down[: down.index("\n  };")]
    assert "if (_chipPositions.has(chip.dataset.modalId))" in down
    # It must take the free path, not the dock-move path.
    free_at = down.index("dragMode = 'free';")
    dock_at = down.index("dragMode = 'move-dock'")
    assert free_at < dock_at


def test_free_drag_survives_having_no_trash_zone():
    # The trash zone is created on touch only; desktop free-move used to throw
    # on a null zone.
    assert "const tz = trashZone ? trashZone.getBoundingClientRect() : null;" in MM
    assert "if (trashZone && inZone !== overTrash)" in MM


def test_snap_zone_does_not_hijack_a_free_chip_drag():
    move = MM[MM.index("const onPointerMove = (e) => {"):]
    move = move[: move.index("\n  };")]
    assert "dragMode !== 'free' && e.pointerType !== 'touch'" in move


def test_positions_are_clamped_on_resize():
    assert "_chipResizeRaf" in MM
    assert "window.addEventListener('resize'" in MM
    assert "Math.min(window.innerWidth - 44, pos.left)" in MM


def test_an_unregistered_window_can_still_be_displaced():
    """Lazily-wired windows are visible before they are registered."""
    body = MM[MM.index("function _fgOpened("):]
    body = body[: body.index("\n}")]
    assert "_AUTO_WIRE[parent]" in body
    assert "_autoRegister(parent)" in body


def test_doc_panel_registers_before_announcing_the_foreground():
    open_fn = DOC[DOC.index("export function openPanel("):]
    open_fn = open_fn[: open_fn.index("\n  }")]
    reg = open_fn.index("_ensureDocChipRegistered();")
    notify = open_fn.index("Modals.notifyOpened('doc-panel')")
    assert reg < notify


def test_transient_dialogs_do_not_displace_windows():
    """`.modal` is also used by styledConfirm/styledPrompt overlays.

    Those are dialogs, not windows: opening a confirm while the editor is open
    must not collapse the editor into a chip.
    """
    body = MM[MM.index("function _canDisplace("):]
    body = body[: body.index("\n}")]
    assert "_WINDOWS[id]" in body and "_AUTO_WIRE[id]" in body
    # Anything outside the window registry is refused.
    assert "return false;" in body


# ── non-modal windows (Notes) ──────────────────────────────────────────────

def test_windows_without_a_modal_element_can_announce_themselves():
    """Notes is a `.notes-pane`, not a `.modal`, so no visibility observer
    exists for it. It dispatches `odysseus:modal-opened`; the manager must
    honour that or Notes can never be collapsed."""
    assert "addEventListener('odysseus:modal-opened'" in MM
    listener = MM[MM.index("addEventListener('odysseus:modal-opened'"):]
    listener = listener[: listener.index("});")]
    assert "_fgOpened(id)" in listener


def test_notes_registers_hooks_before_announcing_the_foreground():
    open_fn = NOTES[NOTES.index("export function openPanel()"):]
    open_fn = open_fn[: open_fn.index("\n  const container")]
    reg = open_fn.index("_ensureNotesChipRegistered();")
    notify = open_fn.index("Modals.notifyOpened('notes-panel')")
    assert reg < notify


def test_notes_has_a_minimize_hook():
    # Modals.minimize() cannot hide a pane that has no #notes-panel element.
    assert "registerMinimizeHook('notes-panel'" in NOTES
    assert "closePanel('down')" in NOTES


def test_notes_hands_the_place_back_and_forgets_on_close():
    close_fn = NOTES[NOTES.index("export function closePanel(direction) {"):]
    close_fn = close_fn[: close_fn.index("\n  // Drop the document keydown")]
    assert "Modals.notifyLeft('notes-panel')" in close_fn
    assert "Modals.forgetForeground('notes-panel')" in close_fn


# ── chips must stay visible above tool windows ─────────────────────────────

def test_dock_sits_above_edge_docked_windows():
    dock = CSS[CSS.index("#minimized-dock {"):]
    dock = dock[: dock.index("}")]
    assert "z-index: var(--z-dock)" in dock
    # Edge-docked tool windows use the split-pane tier in doc view.
    assert "--z-dock: 10040;" in CSS
    assert "--z-split-pane: 10000;" in CSS


def test_free_chips_sit_above_the_dock():
    dock = CSS[CSS.index("#minimized-dock {"):]
    dock = dock[: dock.index("}")]
    assert "z-index: var(--z-dock)" in dock
    assert "String(Z.chipFree)" in MM   # rendered free chip
    assert "String(Z.chipDrag)" in MM   # while dragging
    # CSS/JS tiers are asserted individually in test_z_layers.py; here we only
    # need the free chip to be above the dock.
    z = (ROOT / "static/js/zLayers.js").read_text(encoding="utf-8")
    free = int(z[z.index("chipFree:"):].split(":")[1].split(",")[0])
    dockz = int(z[z.index("dock:"):].split(":")[1].split(",")[0])
    assert free > dockz


# ── the ✕ is a button, not a drag handle ───────────────────────────────────

def test_close_button_has_a_pointer_cursor():
    # Skip the mobile rule that hides it (display: none !important).
    start = CSS.index(".minimized-dock-x {\n      display: inline-flex")
    rule = CSS[start:]
    rule = rule[: rule.index("}")]
    assert "cursor: pointer" in rule
