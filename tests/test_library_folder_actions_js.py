"""Library create/import must target the folder currently open in the UI.

These are source-structure assertions: documentLibrary.js / document.js are
browser-coupled and not importable in pytest (same approach as the sibling
``*_js.py`` suites).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = (ROOT / "static/js/documentLibrary.js").read_text(encoding="utf-8")
DOC = (ROOT / "static/js/document.js").read_text(encoding="utf-8")


def test_library_destination_uses_active_folder_or_root():
    assert "function _libraryDestination()" in LIB
    assert "folderId: _libraryFolderId" in LIB
    assert "Unfiled (root)" in LIB


def test_create_confirmation_names_destination_and_passes_folder():
    start = LIB.index("const createBtn = document.getElementById('doclib-create-btn')")
    end = LIB.index("// Archived toggle", start)
    block = LIB[start:end]

    assert "Do you want to create a new file in" in block
    assert "styledConfirm" in block
    assert "_newDocument({ folderId: destination.folderId })" in block


def test_import_confirmation_names_destination_and_passes_folder():
    start = LIB.index("fileInput.addEventListener('change'")
    end = LIB.index("// Create button", start)
    block = LIB[start:end]

    assert "Do you want to import" in block
    assert "styledConfirm" in block
    assert "libraryImportFiles(files, destination.folderId)" in block
    assert "_libraryImportDestination = _libraryDestination();" in LIB
    assert "_libraryImportDestination || _libraryDestination()" in block


def test_import_requests_forward_folder_id():
    assert "async function libraryImportFiles(fileList, folderId = null)" in LIB
    assert "fd.append('folder_id', folderId)" in LIB
    assert "folder_id: folderId || null" in LIB


def test_create_document_forwards_folder_id():
    assert "export async function newDocument(options = {})" in DOC
    assert "export async function createDocument(sessionId, options = {})" in DOC
    assert "folder_id: options.folderId || null" in DOC
