"""Office import preserves review comments (.docx / .odt).

Background: `.docx` imports silently dropped every comment — markitdown only
reads ``word/document.xml``, while comments live in ``word/comments.xml``. `.odt`
was not readable at all (markitdown 0.1.6 has no ODT converter). These tests
build real containers with zipfile, so they exercise the same code path a user's
file takes — no fixture binaries, no external tools.
"""

import zipfile

import pytest

from src.office_comments import convert_office_with_comments, is_office_comment_format

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
DC = "http://purl.org/dc/elements/1.1/"


def _docx(tmp_path, body_xml: str, comments_xml: str | None, name="doc.docx"):
    doc = (
        f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>'
        f"{body_xml}</w:body></w:document>"
    )
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        if comments_xml is not None:
            z.writestr("word/comments.xml", f'<?xml version="1.0"?><w:comments xmlns:w="{W}">{comments_xml}</w:comments>')
    return str(path)


def _odt(tmp_path, content_xml: str, name="doc.odt"):
    doc = (
        f'<?xml version="1.0"?><office:document-content '
        f'xmlns:office="{OFFICE}" xmlns:text="{TEXT}" xmlns:dc="{DC}">'
        f"<office:body><office:text>{content_xml}</office:text></office:body>"
        f"</office:document-content>"
    )
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", doc)
    return str(path)


def _para(*runs: str) -> str:
    return "<w:p>" + "".join(f"<w:r><w:t>{r}</w:t></w:r>" for r in runs) + "</w:p>"


# ── format routing ──────────────────────────────────────────────────────────

def test_comment_formats_are_docx_and_odt():
    assert is_office_comment_format("a.docx")
    assert is_office_comment_format("a.odt")
    assert is_office_comment_format("A.DOCX")
    assert not is_office_comment_format("a.pdf")
    assert not is_office_comment_format("a.doc")


def test_markitdown_no_longer_owns_docx_and_odt_first():
    from src.markitdown_runtime import COMMENT_AWARE_EXTS, MARKITDOWN_EXTS

    # Not a regression to assert `.docx in MARKITDOWN_EXTS` — what matters is
    # that comment-aware formats are tried BEFORE markitdown.
    assert COMMENT_AWARE_EXTS == {".docx", ".odt"}
    assert ".odt" in MARKITDOWN_EXTS  # supported (as a fallback) at all now


# ── .docx ───────────────────────────────────────────────────────────────────

def test_docx_comment_is_inlined_at_its_anchor(tmp_path):
    body = (
        "<w:p><w:r><w:t>The budget must be approved by Friday.</w:t></w:r>"
        '<w:commentRangeStart w:id="0"/><w:commentRangeEnd w:id="0"/>'
        '<w:r><w:commentReference w:id="0"/></w:r></w:p>'
        + _para("Second paragraph.")
    )
    comments = (
        '<w:comment w:id="0" w:author="A. Rossi" w:date="2026-10-20T10:00:00Z">'
        "<w:p><w:t>Is this the hard deadline?</w:t></w:p></w:comment>"
    )
    md, found = convert_office_with_comments(_docx(tmp_path, body, comments))

    assert "The budget must be approved by Friday." in md
    # Inline marker sits right after the commented paragraph.
    idx_anchor = md.index("The budget must be approved by Friday.")
    idx_marker = md.index("💬 **A. Rossi** (2026-10-20): Is this the hard deadline?")
    idx_second = md.index("Second paragraph.")
    assert idx_anchor < idx_marker < idx_second

    assert len(found) == 1
    assert found[0]["author"] == "A. Rossi"
    assert found[0]["date"] == "2026-10-20"
    assert found[0]["document_level"] is False
    assert found[0]["excerpt"] == "The budget must be approved by Friday."


def test_docx_summary_section_lists_comment_with_excerpt(tmp_path):
    body = (
        "<w:p><w:commentRangeStart w:id=\"0\"/><w:r><w:t>Quoted sentence.</w:t></w:r>"
        '<w:commentRangeEnd w:id="0"/></w:p>'
    )
    comments = (
        '<w:comment w:id="0" w:author="M. Bianchi" w:date="2026-10-22T09:00:00Z">'
        "<w:p><w:t>Clarify this.</w:t></w:p></w:comment>"
    )
    md, _ = convert_office_with_comments(_docx(tmp_path, body, comments))

    assert "## Comments (1)" in md
    assert "> Quoted sentence." in md
    assert "Clarify this." in md


def test_docx_without_comments_has_no_noise(tmp_path):
    md, found = convert_office_with_comments(
        _docx(tmp_path, _para("Just text.") + _para("More text."), None)
    )
    assert found == []
    assert "## Comments" not in md
    assert "💬" not in md
    assert md == "Just text.\n\nMore text."


def test_docx_comment_without_anchor_is_document_level(tmp_path):
    comments = (
        '<w:comment w:id="7" w:author="C. Verdi" w:date="2026-10-23T12:00:00Z">'
        "<w:p><w:t>Missing the appendix.</w:t></w:p></w:comment>"
    )
    md, found = convert_office_with_comments(_docx(tmp_path, _para("Body."), comments))

    assert len(found) == 1
    assert found[0]["document_level"] is True
    assert found[0]["excerpt"] is None
    assert "(on the whole document)" in md


def test_docx_runs_are_not_glued_together(tmp_path):
    # Word splits text into runs; joining them raw produced "Friday.See note."
    body = (
        "<w:p><w:r><w:t>The budget must be approved</w:t></w:r>"
        "<w:r><w:t> by Friday.</w:t></w:r></w:p>"
    )
    md, _ = convert_office_with_comments(_docx(tmp_path, body, None))
    assert md == "The budget must be approved by Friday."


# ── .odt ────────────────────────────────────────────────────────────────────

def test_odt_annotation_is_preserved(tmp_path):
    content = (
        '<text:h text:outline-level="1">Introduction</text:h>'
        "<text:p>The budget must be approved by Friday."
        "<office:annotation>"
        "<dc:creator>M. Bianchi</dc:creator>"
        "<dc:date>2026-10-22T09:00:00</dc:date>"
        "<text:p>Missing the appendix.</text:p>"
        "</office:annotation></text:p>"
    )
    md, found = convert_office_with_comments(_odt(tmp_path, content))

    assert "# Introduction" in md
    assert "💬 **M. Bianchi** (2026-10-22): Missing the appendix." in md
    assert "## Comments (1)" in md
    assert len(found) == 1
    assert found[0]["excerpt"] == "The budget must be approved by Friday."


def test_odt_annotation_body_is_not_duplicated_as_a_paragraph(tmp_path):
    content = (
        "<text:p>Anchor text."
        "<office:annotation><dc:creator>X</dc:creator>"
        "<text:p>Comment body.</text:p></office:annotation></text:p>"
    )
    md, _ = convert_office_with_comments(_odt(tmp_path, content))
    # The annotation body appears in the marker + summary, not as loose text.
    assert md.count("Comment body.") == 2


def test_odt_without_annotations_is_plain(tmp_path):
    md, found = convert_office_with_comments(
        _odt(tmp_path, "<text:p>Only text.</text:p>")
    )
    assert found == []
    assert md == "Only text."
    assert "## Comments" not in md


# ── robustness ──────────────────────────────────────────────────────────────

def test_comment_text_with_markdown_characters_survives(tmp_path):
    body = (
        '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>Anchor.</w:t></w:r>'
        '<w:commentRangeEnd w:id="0"/></w:p>'
    )
    comments = (
        '<w:comment w:id="0" w:author="A" w:date="2026-10-20T10:00:00Z">'
        "<w:p><w:t>Use *bold* and > quote carefully</w:t></w:p></w:comment>"
    )
    md, found = convert_office_with_comments(_docx(tmp_path, body, comments))
    assert "Use *bold* and > quote carefully" in md
    assert found[0]["text"] == "Use *bold* and > quote naturally".replace(
        "naturally", "carefully"
    )


def test_missing_author_and_date_are_tolerated(tmp_path):
    body = (
        '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>x</w:t></w:r>'
        '<w:commentRangeEnd w:id="0"/></w:p>'
    )
    comments = '<w:comment w:id="0"><w:p><w:t>No metadata.</w:t></w:p></w:comment>'
    md, found = convert_office_with_comments(_docx(tmp_path, body, comments))
    assert found[0]["author"] == "(unknown author)"
    assert found[0]["date"] == ""
    assert "(unknown author)" in md


def test_corrupt_container_returns_none(tmp_path):
    bad = tmp_path / "broken.docx"
    bad.write_bytes(b"not a zip")
    assert convert_office_with_comments(str(bad)) is None


def test_unsupported_extension_returns_none(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4")
    assert convert_office_with_comments(str(p)) is None


# ── importer integration ────────────────────────────────────────────────────

def test_legacy_doc_gets_a_clear_message(tmp_path, monkeypatch):
    from src import document_processor

    doc = tmp_path / "old.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0")  # OLE2 magic, not a zip
    out = document_processor._process_office_document(str(doc), "old.doc")
    assert ".docx" in out and "not supported" in out


def test_import_records_comment_count_in_version_summary(tmp_path, monkeypatch):
    """The version history should show that comments survived the import."""
    import src.office_doc as office_doc

    captured = {}

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    class _FakeDb:
        def query(self, *a, **k):
            return _FakeQuery()

        def add(self, obj):
            captured.setdefault("rows", []).append(obj)

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("src.database.SessionLocal", lambda: _FakeDb())
    # `src.agent_tools.document_tools` is imported lazily inside
    # create_office_document, so patch the function on the already-imported
    # module rather than relying on the parent package exposing the attr.
    import src.agent_tools.document_tools as document_tools

    monkeypatch.setattr(document_tools, "set_active_document", lambda *_a, **_k: None)

    office_doc.create_office_document(
        session_id="s", upload_id="u", title="T", body_text="body", comment_count=3
    )
    summaries = [r.summary for r in captured.get("rows", []) if hasattr(r, "summary")]
    assert any("3 comments" in (s or "") for s in summaries)
