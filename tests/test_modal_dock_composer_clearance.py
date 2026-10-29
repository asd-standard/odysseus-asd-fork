from pathlib import Path


CSS = Path("static/style.css").read_text(encoding="utf-8")
INIT_JS = Path("static/js/init.js").read_text(encoding="utf-8")
APP_JS = Path("static/app.js").read_text(encoding="utf-8")
MODAL_MANAGER = Path("static/js/modalManager.js").read_text(encoding="utf-8")


def test_minimized_window_dock_clears_the_composer():
    assert "#minimized-dock {" in CSS
    assert "bottom: var(--composer-clearance, 12px);" in CSS


def test_legacy_modal_dock_is_gone():
    """There is exactly one minimize dock: #minimized-dock in modalManager."""
    assert "#modal-dock {" not in CSS
    assert ".modal-dock-item" not in CSS
    assert ".modal.minimized" not in CSS
    assert "initModalMinimize" not in APP_JS
    assert "modal-dock-item" not in APP_JS
    assert "classList.add('minimized')" not in APP_JS
    # The manager owns the only minimize button injection.
    assert "modal-minimize-btn" in MODAL_MANAGER
    assert "minimize-btn" not in APP_JS


def test_composer_clearance_tracks_input_and_attachment_height():
    assert "const chatBar = document.querySelector('.chat-input-bar');" in INIT_JS
    assert "const attachStrip = document.getElementById('attach-strip');" in INIT_JS
    assert "root.style.setProperty('--composer-clearance', clearance + 'px');" in INIT_JS
