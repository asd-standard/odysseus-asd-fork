"""Window identity contract.

Every tool window — modal or not — carries a stable `data-window-id` matching
its registry id. The manager resolves windows through that attribute, so a
window whose DOM id differs (Notes: `#notes-pane` vs `notes-panel`) is still
found, and a removed feature cannot linger in the registry.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MM = (ROOT / "static/js/modalManager.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")

LIVE_WINDOWS = {
    "calendar-modal",
    "tasks-modal",
    "doclib-modal",
    "memory-modal",
    "notes-panel",
    "email-lib-modal",
    "custom-preset-modal",
    "research-overlay",
    "theme-modal",
    "compare-model-overlay",
    "settings-modal",
    "doc-panel",
}


def _registry_ids():
    block = MM[MM.index("const _WINDOWS = {"):]
    block = block[: block.index("\n};")]
    return set(re.findall(r"^  '([a-z0-9-]+)':", block, re.M))


def test_registry_is_the_live_window_set():
    assert _registry_ids() == LIVE_WINDOWS


def test_removed_features_are_not_in_the_registry():
    for dead in ("cookbook-modal", "gallery-modal", "ge-shortcuts-modal"):
        assert dead not in _registry_ids()
        assert f"'{dead}'" not in MM.split("const _WINDOWS = {")[1].split("\n};")[0]


def test_auto_wire_is_derived_from_the_registry():
    """One declaration per window; the wiring table is not a second list."""
    assert "const _AUTO_WIRE = Object.fromEntries(" in MM
    assert "Object.entries(_WINDOWS)" in MM
    assert "filter(([, w]) => !w.virtual)" in MM
    # The old literal table must not come back.
    assert "const _AUTO_WIRE = {\n" not in MM


def test_identity_is_resolved_through_data_window_id():
    assert "function _windowEl(id)" in MM
    resolver = MM[MM.index("function _windowEl(id)"):]
    resolver = resolver[: resolver.index("\n}")]
    assert '[data-window-id="${id}"]' in resolver
    assert "getElementById(id)" in resolver
    tag = MM[MM.index("function _tagWindow(id)"):]
    tag = tag[: tag.index("\n}")]
    assert "setAttribute('data-window-id', id)" in tag


def test_observers_select_data_window_id():
    observer = MM[MM.index("function _observeAllModals(root)"):]
    observer = observer[: observer.index("\n}")]
    assert "[data-window-id]" in observer
    assert "data-window-id" in observer


def test_only_modal_elements_are_observed():
    """Notes owns its own visibility; a pane must never be treated as a modal."""
    observer = MM[MM.index("function _observeModal(id)"):]
    observer = observer[: observer.index("\n}")]
    assert "modal.classList.contains('modal')" in observer


def test_creators_stamp_the_window_id():
    creators = {
        "static/js/tasks.js": "tasks-modal",
        "static/js/documentLibrary.js": "doclib-modal",
        "static/js/emailLibrary.js": "email-lib-modal",
        "static/js/research/panel.js": "research-overlay",
        "static/js/compare/selector.js": "compare-model-overlay",
    }
    for rel, wid in creators.items():
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert f"setAttribute('data-window-id', '{wid}')" in source, rel


def test_notes_uses_its_registry_id_not_its_dom_id():
    notes = (ROOT / "static/js/notes.js").read_text(encoding="utf-8")
    assert "pane.setAttribute('data-window-id', 'notes-panel')" in notes
    assert "pane.id = 'notes-pane'" in notes


def test_static_modals_declare_their_window_id():
    for wid in ("memory-modal", "theme-modal", "custom-preset-modal", "settings-modal"):
        assert f'data-window-id="{wid}"' in INDEX, wid


def test_runtime_registrations_join_the_same_registry():
    reg = MM[MM.index("export function register(id"):]
    reg = reg[: reg.index("\n}")]
    assert "_WINDOWS[id] = { label: label || id, icon: icon || '' };" in reg
