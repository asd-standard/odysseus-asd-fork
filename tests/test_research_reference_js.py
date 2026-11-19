"""Research cards expose a paste-ready chat reference.

The research panel and Library both need a 'Copy reference' action, and the
composer must ask the server to measure oversized reports before sending.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL = (ROOT / "static/js/research/panel.js").read_text(encoding="utf-8")
LIBRARY = (ROOT / "static/js/documentLibrary.js").read_text(encoding="utf-8")
CHAT = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")


def test_research_panel_copies_an_anchor_reference():
    assert 'data-action="reference"' in PANEL
    assert 'title="Copy reference for chat"' in PANEL
    assert '`[${label}](#research-${job.id})`' in PANEL
    assert "_copyReference(job, btn)" in PANEL


def test_library_offers_copy_reference_in_preview_and_menu():
    assert "function _researchReference(item)" in LIBRARY
    assert "Copy reference" in LIBRARY
    assert "doclib-chat-copy-ref-btn" in LIBRARY
    assert "_copyResearchReference(r)" in LIBRARY


def test_composer_checks_reference_budget_and_sends_the_mode():
    assert "/api/research/refs/inspect" in CHAT
    assert "Compact and send" in CHAT
    assert "Send as-is" in CHAT
    assert "fd.append('research_ref_mode', researchRefMode)" in CHAT
