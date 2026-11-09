"""Preview view-switch scroll preservation.

Markdown maps proportionally between Write and Preview, CSV maps exact source
lines to table rows, and render previews (HTML/SVG/XML iframe) keep independent
scroll positions instead of rebuilding/resetting every time.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_JS = (ROOT / "static/js/document.js").read_text(encoding="utf-8")


def test_markdown_toggle_maps_scroll_ratio_both_ways():
    assert "function _scrollRatio(el)" in DOC_JS
    assert "targetRatio = _scrollRatio(wasActive ? preview : textarea);" in DOC_JS
    assert "_setMarkdownPreviewActive(next, { syncScroll: true });" in DOC_JS


def test_markdown_refresh_preserves_preview_position():
    assert "const ratio = _scrollRatio(preview);" in DOC_JS
    assert "_setMarkdownPreviewActive(true, { remember: false, ratio });" in DOC_JS


def test_csv_parser_tracks_source_lines_per_row():
    assert "const rowLines = [];" in DOC_JS
    assert "rowLines.push(rowLine);" in DOC_JS
    assert "const { rows, rowLines } = parseCSV(" in DOC_JS
    assert 'data-src-line="${rowLines[0] || 1}"' in DOC_JS
    assert 'data-src-line="${rowLines[i] || (rowLines[0] + i)}"' in DOC_JS


def test_csv_toggle_maps_line_to_row_and_back():
    assert "function _scrollCsvToLine(preview, line, fraction = 0)" in DOC_JS
    assert "function _topVisibleCsvAnchor(preview)" in DOC_JS
    assert "_scrollCsvToLine(preview, anchor.line, anchor.fraction)" in DOC_JS
    assert "_scrollTextareaToLine(textarea, anchor.line, anchor.fraction)" in DOC_JS


def test_html_preview_keeps_its_own_scroll_position():
    assert "_saveViewScroll('write', textarea);" in DOC_JS
    assert "iframe._odysseusSrcDoc !== code || iframe._odysseusDocId !== activeDocId" in DOC_JS
    assert "iframe.srcdoc = '';" not in DOC_JS
    assert "_restoreViewScroll('write', textarea);" in DOC_JS
