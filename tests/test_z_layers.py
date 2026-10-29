"""The window stacking scale is declared once and mirrored in CSS.

`static/js/zLayers.js` is the source of truth for JS; `:root` carries the same
values as `--z-*` custom properties. Tests pin both so a new overlay cannot
reintroduce a magic number that lands under the dock.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
Z_JS = (ROOT / "static/js/zLayers.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")
MM = (ROOT / "static/js/modalManager.js").read_text(encoding="utf-8")
TWZ = (ROOT / "static/js/toolWindowZOrder.js").read_text(encoding="utf-8")

CSS_VAR = {
    "windowBase": "--z-window",
    "windowRaised": "--z-window-raised",
    "splitUnderlay": "--z-split-underlay",
    "tileStripe": "--z-tile-stripe",
    "dragGhost": "--z-drag-ghost",
    "edgeHint": "--z-edge-hint",
    "splitPane": "--z-split-pane",
    "splitPaneRaised": "--z-split-pane-raised",
    "portalFloor": "--z-portal",
    "dock": "--z-dock",
    "chipFree": "--z-chip-free",
    "chipDrag": "--z-chip-drag",
}


def _js_values():
    block = Z_JS[Z_JS.index("export const Z = Object.freeze({"):]
    block = block[: block.index("});")]
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"(\w+):\s*(\d+)", block)}


def _css_value(name):
    m = re.search(rf"{re.escape(name)}:\s*(\d+);", CSS)
    assert m, name
    return int(m.group(1))


def test_js_and_css_tiers_match():
    values = _js_values()
    for key, var in CSS_VAR.items():
        assert key in values, key
        assert values[key] == _css_value(var), (key, var)


def test_tiers_are_ordered():
    v = _js_values()
    assert v["windowBase"] < v["windowRaised"] < v["dragGhost"] < v["edgeHint"]
    assert v["edgeHint"] < v["splitPane"] < v["splitPaneRaised"]
    assert v["splitPaneRaised"] < v["portalFloor"] < v["dock"] < v["chipFree"] < v["chipDrag"]


def test_dock_and_split_rules_use_the_scale():
    dock = CSS[CSS.index("#minimized-dock {"):]
    dock = dock[: dock.index("}")]
    assert "z-index: var(--z-dock)" in dock
    assert "z-index: var(--z-split-pane-raised) !important" in CSS
    assert "z-index: var(--z-split-pane) !important" in CSS
    assert "z-index: 10040" not in CSS


def test_chips_read_the_js_scale():
    assert "String(Z.chipFree)" in MM
    assert "String(Z.chipDrag)" in MM
    # No literal chip/dock tiers left in the manager.
    for literal in ("'10040'", "'10050'", "'10060'", "'10030'"):
        assert literal not in MM


def test_portal_menus_clear_a_dragged_chip():
    fn = TWZ[TWZ.index("export function topPortalZ("):]
    fn = fn[: fn.index("\n}")]
    assert "Z.chipDrag" in fn


def test_allocator_resets_when_no_window_is_open():
    fn = MM[MM.index("function _bringToFront(modal)"):]
    fn = fn[: fn.index("\n}")]
    assert "if (!anyOtherVisible) _modalTopZ = Z.windowRaised;" in fn
