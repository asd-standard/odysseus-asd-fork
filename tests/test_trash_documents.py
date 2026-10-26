"""Library Trash — document soft-delete, restore, permanent delete, purge.

Exercises the real document route handlers against a temp SQLite DB, following
the direct-handler pattern used by ``test_library_folders.py``.
"""
import asyncio
import os
import sqlite3
import tempfile
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
import routes.document.document_routes as droutes
from core.database import Document, DocumentVersion, Folder, utcnow_naive
from src import trash_service as trash

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _bind_test_db(monkeypatch):
    monkeypatch.setattr(droutes, "SessionLocal", _TS)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    # The temp DB is shared by this module; start each test from a clean slate.
    db = _TS()
    try:
        db.query(DocumentVersion).delete()
        db.query(Document).delete()
        db.query(Folder).delete()
        db.commit()
    finally:
        db.close()
    yield


def _req(user="tester"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _endpoint(method, path):
    router = droutes.setup_document_routes(MagicMock(), None)
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _call(method, path, *args, **kwargs):
    return asyncio.run(_endpoint(method, path)(*args, **kwargs))


def _make_doc(owner="tester", title="Doc", archived=False, folder_id=None,
              is_active=True, deleted_at=None):
    db = _TS()
    try:
        doc = Document(
            id=str(uuid.uuid4()),
            title=title,
            language="markdown",
            current_content="hello",
            version_count=1,
            is_active=is_active,
            deleted_at=deleted_at,
            archived=archived,
            owner=owner,
            folder_id=folder_id,
        )
        db.add(doc)
        db.commit()
        return doc.id
    finally:
        db.close()


def _db_get(model, row_id):
    db = _TS()
    try:
        return db.query(model).filter(model.id == row_id).first()
    finally:
        db.close()


def test_delete_moves_to_trash_and_library_hides_it():
    doc_id = _make_doc(title="Quarterly report")

    result = _call("DELETE", "/api/document/{doc_id}", _req(), doc_id)
    assert result["status"] == "trashed"

    row = _db_get(Document, doc_id)
    assert row.is_active is False
    assert row.deleted_at is not None

    listed = _call("GET", "/api/documents/trash", _req(), None, "recent", 0, 50)
    assert [d["id"] for d in listed["documents"]] == [doc_id]
    assert listed["documents"][0]["days_left"] == 15
    assert listed["retention_days"] == 15

    library = _call(
        "GET", "/api/documents/library", _req(),
        None, None, "recent", 0, 50, False, None,
    )
    assert all(d["id"] != doc_id for d in library["documents"])


def test_restore_preserves_folder_and_archive_state():
    db = _TS()
    try:
        folder = Folder(id=str(uuid.uuid4()), name="Reports", owner="tester", sort_order=0)
        db.add(folder)
        db.commit()
        folder_id = folder.id
    finally:
        db.close()
    doc_id = _make_doc(archived=True, folder_id=folder_id)
    _call("DELETE", "/api/document/{doc_id}", _req(), doc_id)

    result = _call("POST", "/api/document/{doc_id}/restore", _req(), doc_id)
    assert result == {"ok": True, "id": doc_id, "archived": True, "folder_id": folder_id}

    row = _db_get(Document, doc_id)
    assert row.is_active is True
    assert row.deleted_at is None
    assert row.archived is True
    assert row.folder_id == folder_id


def test_restore_rejects_active_document():
    doc_id = _make_doc()
    with pytest.raises(HTTPException) as exc:
        _call("POST", "/api/document/{doc_id}/restore", _req(), doc_id)
    assert exc.value.status_code == 409


def test_permanent_delete_removes_versions():
    doc_id = _make_doc()
    db = _TS()
    try:
        db.add(DocumentVersion(
            id=str(uuid.uuid4()),
            document_id=doc_id,
            version_number=1,
            content="hello",
            source="ai",
        ))
        db.commit()
    finally:
        db.close()
    _call("DELETE", "/api/document/{doc_id}", _req(), doc_id)

    result = _call("DELETE", "/api/document/{doc_id}/permanent", _req(), doc_id)
    assert result["status"] == "deleted"
    assert _db_get(Document, doc_id) is None
    db = _TS()
    try:
        assert db.query(DocumentVersion).filter(DocumentVersion.document_id == doc_id).count() == 0
    finally:
        db.close()


def test_permanent_delete_rejects_active_document():
    doc_id = _make_doc()
    with pytest.raises(HTTPException) as exc:
        _call("DELETE", "/api/document/{doc_id}/permanent", _req(), doc_id)
    assert exc.value.status_code == 409


def test_trash_is_owner_scoped():
    mine = _make_doc(owner="tester", title="Mine")
    other = _make_doc(owner="someone-else", title="Theirs")
    _call("DELETE", "/api/document/{doc_id}", _req("tester"), mine)
    _call("DELETE", "/api/document/{doc_id}", _req("someone-else"), other)

    listed = _call("GET", "/api/documents/trash", _req("tester"), None, "recent", 0, 50)
    assert [d["id"] for d in listed["documents"]] == [mine]

    with pytest.raises(HTTPException) as exc:
        _call("POST", "/api/document/{doc_id}/restore", _req("tester"), other)
    assert exc.value.status_code == 404


def test_empty_trash_only_deletes_callers_documents():
    mine = _make_doc(owner="tester")
    other = _make_doc(owner="someone-else")
    _call("DELETE", "/api/document/{doc_id}", _req("tester"), mine)
    _call("DELETE", "/api/document/{doc_id}", _req("someone-else"), other)

    result = _call("POST", "/api/documents/trash/empty", _req("tester"))
    assert result["deleted"] == 1
    assert _db_get(Document, mine) is None
    assert _db_get(Document, other) is not None


def test_purge_expired_documents_respects_cutoff_and_owner():
    db = _TS()
    try:
        old = Document(id=str(uuid.uuid4()), title="old", owner="tester",
                       is_active=False, deleted_at=utcnow_naive() - timedelta(days=16))
        fresh = Document(id=str(uuid.uuid4()), title="fresh", owner="tester",
                         is_active=False, deleted_at=utcnow_naive() - timedelta(days=14))
        other = Document(id=str(uuid.uuid4()), title="other", owner="someone-else",
                         is_active=False, deleted_at=utcnow_naive() - timedelta(days=30))
        db.add_all([old, fresh, other])
        db.commit()
        old_id, fresh_id, other_id = old.id, fresh.id, other.id
    finally:
        db.close()

    assert trash.purge_expired_documents(db=_TS(), owner="tester") == 1
    assert _db_get(Document, old_id) is None
    assert _db_get(Document, fresh_id) is not None
    assert _db_get(Document, other_id) is not None


def test_trash_metadata_counts_down():
    deleted = utcnow_naive() - timedelta(days=3)
    meta = trash.trash_metadata(deleted)
    assert meta["days_left"] == 12
    assert meta["deleted_at"].endswith("Z")
    assert meta["expires_at"].endswith("Z")


def test_retention_days_defaults_and_clamps(monkeypatch):
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 15)
    assert trash.retention_days() == 15
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 0)
    assert trash.retention_days() == 1
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 99999)
    assert trash.retention_days() == 3650


def test_migration_adds_and_backfills_deleted_at(monkeypatch, tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE documents (id VARCHAR PRIMARY KEY, title VARCHAR, is_active BOOLEAN)")
    conn.execute("INSERT INTO documents (id, title, is_active) VALUES ('d1', 'legacy', 0)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{db_path}")
    cdb._migrate_add_document_deleted_at_column()

    conn = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(documents)")]
        assert "deleted_at" in columns
        value = conn.execute("SELECT deleted_at FROM documents WHERE id = 'd1'").fetchone()[0]
        assert value
    finally:
        conn.close()
