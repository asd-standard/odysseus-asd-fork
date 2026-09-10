"""Folder-aware list_library_files / read_library_file.

The agent tools resolve against the SQLite `folders` table + `Document.folder_id`
so the LLM can list a folder's contents and bulk-read every file in a folder.
Exercises the real tool classes against a temp SQLite DB (owner-scoped), with
the personal-docs path stubbed out so only the SQLite side is under test.
"""

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
import src.agent_tools.library_tools as lt
from core.database import Document, DocumentVersion, Folder

_TS = None


@pytest.fixture(autouse=True)
def _bind(monkeypatch):
    # Fresh in-memory DB per test (StaticPool shares one connection across
    # sessions so each test is fully isolated from the previous one).
    global _TS
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    cdb.Base.metadata.create_all(engine)
    _TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(lt, "SessionLocal", _TS)
    yield


def _folder(name, owner="tester", parent_id=None):
    fid = str(uuid.uuid4())
    db = _TS()
    try:
        db.add(Folder(id=fid, name=name, parent_id=parent_id, owner=owner, sort_order=0))
        db.commit()
        return fid
    finally:
        db.close()


def _doc(title, owner="tester", folder_id=None, content="body"):
    db = _TS()
    try:
        doc = Document(
            id=str(uuid.uuid4()), title=title, language="markdown",
            current_content=content, version_count=1, is_active=True,
            owner=owner, folder_id=folder_id,
        )
        db.add(doc)
        db.commit()
        return doc.id
    finally:
        db.close()


def _call_list(args, owner="tester"):
    return lt.ListLibraryFilesTool().execute(json.dumps(args or {}), {"owner": owner})


def _call_read(args, owner="tester"):
    return lt.ReadLibraryFileTool().execute(json.dumps(args or {}), {"owner": owner})


def _call_make(args, owner="tester"):
    return lt.MakeLibraryFileTool().execute(json.dumps(args or {}), {"owner": owner})


async def test_list_root_shows_folders_and_unfiled_only():
    work = _folder("Work")
    _doc("Filed doc", folder_id=work, content="in work")
    _doc("Unfiled doc", content="loose")

    out = await _call_list({})
    assert out["exit_code"] == 0
    assert "Folders" in out["output"]
    assert "Work" in out["output"]
    assert "Unfiled documents" in out["output"]
    assert "Unfiled doc" in out["output"]
    assert "Filed doc" not in out["output"]


async def test_list_folder_by_name_lists_contents():
    work = _folder("Work")
    _folder("Sub", parent_id=work)
    _doc("Alpha", folder_id=work, content="a")
    _doc("Other", content="elsewhere")

    out = await _call_list({"folder": "Work"})
    assert out["exit_code"] == 0
    assert "Folder `Work`" in out["output"]
    assert "Alpha" in out["output"]
    assert "Sub" in out["output"]
    assert "Other" not in out["output"]


async def test_list_folder_by_id_matches_name():
    work = _folder("Work")
    _doc("Inside", folder_id=work, content="x")

    out = await _call_list({"folder": work})
    assert "Inside" in out["output"]


async def test_list_folder_case_insensitive_name():
    _folder("Work")
    _doc("Inside", folder_id=None, content="x")
    out = await _call_list({"folder": "work"})
    assert out["exit_code"] == 0


async def test_list_folder_not_found():
    _folder("Work")
    out = await _call_list({"folder": "Nope"})
    assert out["exit_code"] == 1
    assert "not found" in out["error"]
    assert "Work" in out["error"]  # hint lists available folders


async def test_list_folder_owner_isolation():
    _folder("AliceFolder", owner="alice")
    out = await _call_list({"folder": "AliceFolder"}, owner="bob")
    assert out["exit_code"] == 1
    assert "not found" in out["error"]


async def test_read_folder_bulk_concatenates_all_files():
    work = _folder("Work")
    _doc("One", folder_id=work, content="content one")
    _doc("Two", folder_id=work, content="content two")

    out = await _call_read({"folder": "Work"})
    assert out["exit_code"] == 0
    assert "### One" in out["output"]
    assert "### Two" in out["output"]
    assert "content one" in out["output"]
    assert "content two" in out["output"]
    assert out["document"]["file_count"] == 2


async def test_read_folder_paginates_long_content():
    work = _folder("Work")
    _doc("Big", folder_id=work, content="x" * 25000)

    out = await _call_read({"folder": "Work"})
    assert out["document"]["truncated"] is True
    assert out["document"]["next_offset"] is not None

    rest = await _call_read({"folder": "Work", "offset": out["document"]["next_offset"]})
    assert rest["exit_code"] == 0
    assert rest["document"]["truncated"] is False


async def test_read_folder_empty():
    work = _folder("Work")
    out = await _call_read({"folder": work})
    assert out["exit_code"] == 0
    assert "is empty" in out["output"]
    assert out["document"]["file_count"] == 0


async def test_read_folder_not_found():
    out = await _call_read({"folder": "Missing"})
    assert out["exit_code"] == 1
    assert "not found" in out["error"]


async def test_read_single_by_name_still_works():
    _doc("Solo", content="solo content")
    out = await _call_read({"name": "Solo"})
    assert out["exit_code"] == 0
    assert "solo content" in out["output"]


async def test_read_requires_name_or_folder():
    out = await _call_read({})
    assert out["exit_code"] == 1
    assert "name" in out["error"] and "folder" in out["error"]


async def test_make_library_file_creates_sqlite_document():
    out = await _call_make({"name": "notes.md", "content": "hello library"})
    assert out["exit_code"] == 0
    doc_id = out["document"]["id"]

    db = _TS()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        assert doc is not None
        assert doc.title == "notes.md"
        assert doc.language == "markdown"
        assert doc.current_content == "hello library"
        assert doc.owner == "tester"
        assert doc.folder_id is None
        assert doc.is_active is True
        assert doc.version_count == 1

        ver = db.query(DocumentVersion).filter(DocumentVersion.document_id == doc_id).first()
        assert ver is not None
        assert ver.version_number == 1
        assert ver.content == "hello library"
    finally:
        db.close()

    # It must be discoverable by the other two tools.
    listed = await _call_list({})
    assert "notes.md" in listed["output"]

    read = await _call_read({"name": "notes.md"})
    assert read["exit_code"] == 0
    assert "hello library" in read["output"]


async def test_make_library_file_detects_language_from_extension():
    out = await _call_make({"name": "script.py", "content": "print(1)"})
    assert out["exit_code"] == 0
    assert out["document"]["language"] == "python"


async def test_make_library_file_requires_name_and_content():
    out = await _call_make({"content": "no name"})
    assert out["exit_code"] == 1
    assert "name" in out["error"]

    out = await _call_make({"name": "x.md"})
    assert out["exit_code"] == 1
    assert "content" in out["error"]
