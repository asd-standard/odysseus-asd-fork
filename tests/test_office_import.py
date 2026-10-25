"""Office import: rich body from mammoth + comment anchors from office_comments.

The failure this guards against: importing a commented .docx through the
Library produced a document with no comments, because the browser ran mammoth
(which cannot read `word/comments.xml`) and POSTed the result as plain content.
Comments were discarded before the file ever reached the backend.

`import_office_document()` is the single entry point used by the Library route,
chat attachments and the personal-docs indexer, so all three behave identically.
"""

import zipfile
from pathlib import Path

import pytest

from src.doc_import import (
    OfficeImportError,
    import_office_document,
    merge_comments,
    nonprintable_ratio,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
DC = "http://purl.org/dc/elements/1.1/"

ROOT = Path(__file__).resolve().parents[1]


def _docx(tmp_path, body: str, comments: str | None = None, name="d.docx"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
        )
        if comments is not None:
            z.writestr(
                "word/comments.xml",
                f'<?xml version="1.0"?><w:comments xmlns:w="{W}">{comments}</w:comments>',
            )
    return str(path)


def _para(*runs: str, comment_id: str | None = None) -> str:
    body = "".join(f"<w:r><w:t>{r}</w:t></w:r>" for r in runs)
    if comment_id is not None:
        body = (
            f'<w:commentRangeStart w:id="{comment_id}"/>'
            + body
            + f'<w:commentRangeEnd w:id="{comment_id}"/>'
            + f'<w:r><w:commentReference w:id="{comment_id}"/></w:r>'
        )
    return f"<w:p>{body}</w:p>"


def _comment(cid, text, author="A. Rossi", date="2026-10-20T10:00:00Z"):
    return (
        f'<w:comment w:id="{cid}" w:author="{author}" w:date="{date}">'
        f"<w:p><w:t>{text}</w:t></w:p></w:comment>"
    )


# ── the regression ─────────────────────────────────────────────────────────

def test_import_keeps_comments(tmp_path):
    path = _docx(
        tmp_path,
        _para("The budget must be approved by Friday.", comment_id="0")
        + _para("Second paragraph."),
        _comment("0", "Is this the hard deadline?"),
    )
    result = import_office_document(path)

    assert result.comment_count == 1
    assert "💬 **A. Rossi** (2026-10-20): Is this the hard deadline?" in result.markdown
    assert "## Comments (1)" in result.markdown
    assert result.warnings == []


def test_comment_is_placed_after_its_own_paragraph(tmp_path):
    path = _docx(
        tmp_path,
        _para("AAA first paragraph.", comment_id="0")
        + _para("BBB second paragraph.")
        + _para("CCC third paragraph."),
        _comment("0", "Note on the first."),
    )
    md = import_office_document(path).markdown

    first = md.index("AAA first paragraph.")
    marker = md.index("💬 **A. Rossi**")
    second = md.index("BBB second paragraph.")
    assert first < marker < second


def test_multiple_comments_do_not_cascade(tmp_path):
    """Inserting markers shifts later indices — placements must be resolved
    against the unmodified block list, not while mutating it."""
    body = (
        _para("Para one.", comment_id="0")
        + _para("Para two.")
        + _para("Para three.", comment_id="1")
        + _para("Para four.", comment_id="2")
    )
    comments = _comment("0", "on one") + _comment("1", "on three") + _comment("2", "on four")
    md = import_office_document(_docx(tmp_path, body, comments)).markdown

    assert md.count("💬") == 3
    assert md.index("on one") < md.index("Para two.")
    assert md.index("Para two.") < md.index("on three") < md.index("Para four.")
    assert md.index("on four") < md.index("## Comments")


def test_document_level_comment_has_no_inline_marker(tmp_path):
    path = _docx(
        tmp_path,
        _para("Body text."),
        _comment("5", "Missing the appendix."),
    )
    md = import_office_document(path).markdown
    assert "on the whole document" in md
    # A document-level comment is never inlined (there is no paragraph to
    # attach it to); it appears only in the summary.
    assert "> 💬" not in md


# ── rich body ──────────────────────────────────────────────────────────────

def test_tables_and_bold_survive_when_mammoth_is_available(tmp_path):
    pytest.importorskip("subprocess")
    from src.docx_render import mammoth_available

    if not mammoth_available():
        pytest.skip("node/mammoth not available")

    table = (
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Header A</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>Header B</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    bold = "<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Bold text</w:t></w:r></w:p>"
    md = import_office_document(_docx(tmp_path, table + bold)).markdown

    assert "| Header A | Header B |" in md
    assert "**Bold text**" in md


def test_falls_back_when_mammoth_is_unavailable(tmp_path, monkeypatch):
    import src.docx_render as render

    monkeypatch.setattr(render, "mammoth_available", lambda: False)
    path = _docx(tmp_path, _para("Plain body.", comment_id="0"), _comment("0", "note"))
    result = import_office_document(path)

    assert result.comment_count == 1
    assert any("plain text extractor" in w for w in result.warnings)
    assert "💬" in result.markdown


# ── rejected input ─────────────────────────────────────────────────────────

def test_legacy_doc_named_docx_is_rejected(tmp_path):
    path = tmp_path / "old.docx"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)

    with pytest.raises(OfficeImportError) as ei:
        import_office_document(str(path))
    assert ".doc" in str(ei.value) and "Save As" in str(ei.value)


def test_non_zip_docx_is_rejected(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"this is not a zip at all")
    with pytest.raises(OfficeImportError):
        import_office_document(str(path))


def test_unsupported_extension_is_rejected(tmp_path):
    path = tmp_path / "x.pdf"
    path.write_bytes(b"%PDF")
    with pytest.raises(OfficeImportError):
        import_office_document(str(path))


def test_empty_document_is_rejected(tmp_path):
    path = _docx(tmp_path, "")  # no paragraphs at all
    with pytest.raises(OfficeImportError):
        import_office_document(path)


def test_junk_guard_flags_binary_content():
    assert nonprintable_ratio("normal prose text") < 0.05
    assert nonprintable_ratio("\x00\x01\x02\x03binary") > 0.3


# ── ODT ────────────────────────────────────────────────────────────────────

def test_odt_import_keeps_annotations(tmp_path):
    content = (
        f'<?xml version="1.0"?><office:document-content xmlns:office="{OFFICE}" '
        f'xmlns:text="{TEXT}" xmlns:dc="{DC}"><office:body><office:text>'
        "<text:p>Anchor sentence."
        "<office:annotation><dc:creator>M. Bianchi</dc:creator>"
        "<dc:date>2026-10-22T09:00:00</dc:date>"
        "<text:p>Note here.</text:p></office:annotation></text:p>"
        "</office:text></office:body></office:document-content>"
    )
    path = tmp_path / "d.odt"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("content.xml", content)

    result = import_office_document(str(path))
    assert result.comment_count == 1
    assert "💬 **M. Bianchi** (2026-10-22): Note here." in result.markdown


# ── merge unit behaviour ───────────────────────────────────────────────────

def test_merge_reports_unanchored_comment_instead_of_misanchoring():
    md = "Block one.\n\nBlock two."
    comments = [{
        "id": 9, "author": "X", "date": "", "text": "orphan note",
        "excerpt": "text that is not present", "paragraph_index": 1,
        "document_level": False,
    }]
    merged, warnings = merge_comments(md, comments)

    assert any("could not be anchored" in w for w in warnings)
    # Not injected inline at a wrong block...
    assert "> 💬" not in merged
    # ...but still listed in the summary so nothing is lost.
    assert "orphan note" in merged


def test_merge_handles_merged_paragraphs_from_the_renderer():
    """Mammoth can merge several Word paragraphs into one block; the excerpt
    then sits mid-block rather than at its start."""
    md = "First sentence. Second sentence with the anchor. Third sentence."
    comments = [{
        "id": 1, "author": "X", "date": "2026-01-01",
        "text": "note", "excerpt": "Second sentence with the anchor.",
        "paragraph_index": 3,  # out of range on purpose
        "document_level": False,
    }]
    merged, warnings = merge_comments(md, comments)
    assert warnings == []
    assert "💬" in merged


def test_every_entry_point_uses_the_shared_importer():
    """All three import paths must agree, or comments work in one and not others."""
    processor = (ROOT / "src/document_processor.py").read_text(encoding="utf-8")
    personal = (ROOT / "src/personal_docs.py").read_text(encoding="utf-8")
    routes = (ROOT / "routes/personal_routes.py").read_text(encoding="utf-8")
    doc_routes = (ROOT / "routes/document/document_routes.py").read_text(encoding="utf-8")

    assert "import_office_document" in processor
    assert "import_office_document" in personal
    assert "import_office_document" in routes
    assert "import_office_document" in doc_routes


def test_browser_no_longer_converts_docx():
    """The client-side mammoth path silently dropped comments — it must stay gone."""
    lib = (ROOT / "static/js/documentLibrary.js").read_text(encoding="utf-8")
    # No loader and no conversion call — only the explanatory comments remain.
    assert "ensureMammoth" not in lib
    assert "convertToHtml" not in lib
    assert "mammoth.browser.min.js" not in lib
    assert "/api/documents/import-office" in lib
