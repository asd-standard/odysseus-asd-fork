"""Footer Download must follow the selected document type.

The document footer's up-arrow menu used to offer a hardcoded
"Export Markdown" entry even when the document was Python/JS/CSV. The
Download entry now derives its extension from #doc-language-select so the
label and the saved file always match the type the user picked.

Keep these as static checks because document.js is browser-coupled and not
importable in pytest.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_JS = (ROOT / "static/js/document.js").read_text(encoding="utf-8")


def test_download_extension_map_covers_editor_types():
    assert "const _DOC_EXT_BY_LANG = {" in DOC_JS
    for mapping in (
        "javascript: '.js'",
        "python: '.py'",
        "markdown: '.md'",
        "typescript: '.ts'",
        "csharp: '.cs'",
        "svg: '.svg'",
        "toml: '.toml'",
        "csv: '.csv'",
        "email: '.md'",
    ):
        assert mapping in DOC_JS


def test_unknown_language_falls_back_to_txt():
    assert "return _DOC_EXT_BY_LANG[(lang || '').toLowerCase()] || '.txt';" in DOC_JS


def test_download_flushes_pending_edits_before_building_the_blob():
    start = DOC_JS.index("async function exportDocument()")
    end = DOC_JS.index('// "Import from device"', start)
    body = DOC_JS[start:end]
    assert "_saveActiveDocBeforeExport()" in body
    assert "new Blob([textarea.value]" in body


def test_export_menu_uses_selected_extension_and_drops_export_markdown():
    assert "label: `Download ${ext}`" in DOC_JS
    assert "label: 'Export Markdown'" not in DOC_JS
    assert "const ext = _docExtensionForLang(lang);" in DOC_JS


def test_pdf_language_routes_to_the_filled_pdf_route():
    start = DOC_JS.index("async function exportDocument()")
    end = DOC_JS.index('// "Import from device"', start)
    body = DOC_JS[start:end]
    assert "lang.toLowerCase() === 'pdf'" in body
    assert "await _downloadFilledPdf()" in body
    assert "label: 'Download .pdf', fn: _downloadFilledPdf" in DOC_JS
