"""Keep editor selection highlights theme-aware and non-duplicated.

The native ::selection used to paint the fixed blue --color-accent (#00aaff)
regardless of theme. It must use the per-theme accent token (--red) instead.
The pinned AI-selection overlay is measured against a mirror element and can
drift slightly, so it must not paint a filled band over the text — only the
left rule remains.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    start = STYLE_CSS.index(selector + " {")
    end = STYLE_CSS.index("}", start) + 1
    return STYLE_CSS[start:end]


def test_native_selection_uses_theme_accent():
    rule = _rule(".doc-editor-textarea::selection")
    assert "var(--red)" in rule
    assert "var(--color-accent)" not in rule


def test_pinned_overlay_is_not_drawn():
    rule = _rule(".doc-selection-overlay")
    assert "display: none;" in rule
    assert "border-left" not in rule
    assert "background" not in rule


def test_selection_renderer_does_not_create_overlay_rects():
    doc_js = (ROOT / "static/js/document.js").read_text(encoding="utf-8")
    assert "overlay.className = 'doc-selection-overlay'" not in doc_js
    assert "function _measurePos" not in doc_js


def test_theme_application_sets_red_token():
    theme_js = (ROOT / "static/js/theme.js").read_text(encoding="utf-8")
    assert "s.setProperty('--red', colors.red);" in theme_js
