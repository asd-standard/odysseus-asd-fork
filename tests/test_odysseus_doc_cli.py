"""The standalone `office_comments` CLI (also installed as `odysseus-doc`).

Runs the real script as a subprocess against in-memory-built containers, so it
covers argument parsing, exit codes, and output exactly as a user would hit it
from bash.
"""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "office_comments"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
DC = "http://purl.org/dc/elements/1.1/"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def commented_docx(tmp_path):
    body = (
        "<w:p><w:r><w:t>The budget must be approved by Friday.</w:t></w:r>"
        '<w:commentRangeStart w:id="0"/><w:commentRangeEnd w:id="0"/>'
        '<w:r><w:commentReference w:id="0"/></w:r></w:p>'
        "<w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p>"
    )
    comments = (
        '<w:comment w:id="0" w:author="A. Rossi" w:date="2026-10-20T10:00:00Z">'
        "<w:p><w:t>Is this the hard deadline?</w:t></w:p></w:comment>"
    )
    path = tmp_path / "demo.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
        )
        z.writestr(
            "word/comments.xml",
            f'<?xml version="1.0"?><w:comments xmlns:w="{W}">{comments}</w:comments>',
        )
    return str(path)


@pytest.fixture
def annotated_odt(tmp_path):
    content = (
        '<text:h text:outline-level="1">Intro</text:h>'
        "<text:p>Anchor sentence."
        "<office:annotation><dc:creator>M. Bianchi</dc:creator>"
        "<dc:date>2026-10-22T09:00:00</dc:date>"
        "<text:p>Note here.</text:p></office:annotation></text:p>"
    )
    path = tmp_path / "demo.odt"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("content.xml",
                   f'<?xml version="1.0"?><office:document-content xmlns:office="{OFFICE}" '
                   f'xmlns:text="{TEXT}" xmlns:dc="{DC}"><office:body><office:text>'
                   f"{content}</office:text></office:body></office:document-content>")
    return str(path)


def test_convert_prints_markdown_with_comments(commented_docx):
    r = _run("convert", commented_docx)
    assert r.returncode == 0, r.stderr
    assert "The budget must be approved by Friday." in r.stdout
    assert "💬 **A. Rossi** (2026-10-20): Is this the hard deadline?" in r.stdout
    assert "## Comments (1)" in r.stdout


def test_convert_writes_to_output_file(commented_docx, tmp_path):
    out = tmp_path / "out.md"
    r = _run("convert", commented_docx, "-o", str(out))
    assert r.returncode == 0, r.stderr
    assert "wrote" in r.stderr
    text = out.read_text(encoding="utf-8")
    assert "💬 **A. Rossi**" in text


def test_convert_no_comments_strips_them(commented_docx):
    r = _run("convert", commented_docx, "--no-comments")
    assert r.returncode == 0, r.stderr
    assert "💬" not in r.stdout
    assert "## Comments" not in r.stdout
    assert "The budget must be approved by Friday." in r.stdout
    assert "Second paragraph." in r.stdout


def test_comments_subcommand_lists_anchors(commented_docx):
    r = _run("comments", commented_docx)
    assert r.returncode == 0, r.stderr
    assert "A. Rossi (2026-10-20): Is this the hard deadline?" in r.stdout
    assert "on: The budget must be approved by Friday." in r.stdout


def test_info_subcommand(commented_docx):
    r = _run("info", commented_docx)
    assert r.returncode == 0, r.stderr
    assert "format:" in r.stdout and "docx" in r.stdout
    assert "comments:" in r.stdout and "1" in r.stdout


def test_odt_is_supported(annotated_odt):
    r = _run("convert", annotated_odt)
    assert r.returncode == 0, r.stderr
    assert "# Intro" in r.stdout
    assert "💬 **M. Bianchi** (2026-10-22): Note here." in r.stdout


def test_legacy_doc_explains_how_to_convert(tmp_path):
    p = tmp_path / "old.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0")
    r = _run("convert", str(p))
    assert r.returncode == 1
    assert ".docx" in r.stderr and "not supported" in r.stderr


def test_unsupported_format_exits_1(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4")
    r = _run("convert", str(p))
    assert r.returncode == 1
    assert "unsupported format" in r.stderr


def test_missing_file_exits_1(tmp_path):
    r = _run("convert", str(tmp_path / "nope.docx"))
    assert r.returncode == 1
    assert "no such file" in r.stderr


def test_corrupt_container_exits_1(tmp_path):
    p = tmp_path / "bad.docx"
    p.write_bytes(b"not a zip")
    r = _run("convert", str(p))
    assert r.returncode == 1
    assert "could not parse" in r.stderr


def test_usage_error_exits_2():
    r = _run()  # no subcommand
    assert r.returncode == 2


def test_check_succeeds_for_supported_file(commented_docx):
    r = _run("check", commented_docx)
    assert r.returncode == 0
    assert r.stdout == ""


def test_excerpt_helper_truncates_on_a_word_boundary():
    long_text = (
        "The budget must be approved by Friday and communicated to every "
        "department before the end of the quarter, which is quite a long sentence."
    )
    r = _run("excerpt", long_text, "--limit", "40")
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    assert out.endswith("…")
    assert len(out) <= 41
    assert not out[:-1].endswith(" ")


def test_comments_json_is_machine_readable(commented_docx):
    import json as _json

    r = _run("comments", commented_docx, "--json")
    assert r.returncode == 0, r.stderr
    data = _json.loads(r.stdout)
    assert data[0]["author"] == "A. Rossi"
    assert data[0]["excerpt"] == "The budget must be approved by Friday."


def test_info_json(commented_docx):
    import json as _json

    r = _run("info", commented_docx, "--json")
    assert r.returncode == 0, r.stderr
    data = _json.loads(r.stdout)
    assert data["format"] == "docx"
    assert data["comments"] == 1


def test_installed_command_is_a_symlink_to_the_repo_script():
    """Installed the same way as `transcribe`: a symlink in ~/.local/bin."""
    installed = Path.home() / ".local" / "bin" / "office_comments"
    if not installed.exists():
        pytest.skip("office_comments not installed on this machine")
    assert installed.is_symlink()
    target = installed.resolve()
    assert target.name == "office_comments"
    assert target.parent.name == "scripts"
    # The script must still run when invoked through the symlink: its _lib
    # import uses realpath for exactly this reason.
    r = subprocess.run(
        [str(installed), "--version"], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr


# ── library helpers (what the CLI is built on) ──────────────────────────────

def test_library_helpers_are_importable_and_consistent(commented_docx, annotated_odt):
    from src.office_comments import (
        convert_office_with_comments,
        extract_excerpt,
        is_office_comment_format,
        list_comments,
        to_markdown,
    )

    markdown, comments = convert_office_with_comments(commented_docx)
    assert list_comments(commented_docx) == comments
    assert to_markdown(commented_docx) == markdown
    assert to_markdown(commented_docx, include_comments=False).find("💬") == -1
    assert is_office_comment_format(commented_docx)
    assert is_office_comment_format(annotated_odt)
    assert extract_excerpt("short text") == "short text"
    assert list_comments("/nonexistent/x.docx") is None
