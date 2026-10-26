"""Library folders — folder CRUD, nesting, cycle guard, owner isolation.

Exercises the REAL folder route handlers (routes/document/folder_routes.py)
plus the folder-aware PATCH on documents, against a temp SQLite DB — the same
direct-handler pattern used by test_document_close_clears_active_route.py.
"""

import tempfile
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from unittest.mock import MagicMock

from fastapi import HTTPException

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
import routes.document.document_routes as droutes
import routes.document.folder_routes as froutes
from core.database import Document, Folder
from core.database import Session as DbSession
from routes.document_helpers import DocumentPatch

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
    # Route handlers read `SessionLocal` from their module global. Bind both
    # modules to this file's temp DB for the duration of each test, then let
    # monkeypatch restore the originals so sibling test modules (which also
    # patch these globals) are not clobbered across collection.
    monkeypatch.setattr(droutes, "SessionLocal", _TS)
    monkeypatch.setattr(froutes, "SessionLocal", _TS)
    yield


def _req(user="tester"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _endpoint(method, path):
    router = froutes.setup_folder_routes()
    for r in router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _doc_endpoint(method, path):
    router = droutes.setup_document_routes(MagicMock(), None)
    for r in router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _make_folder(name, parent_id=None, owner="tester"):
    fid = str(uuid.uuid4())
    db = _TS()
    try:
        db.add(Folder(id=fid, name=name, parent_id=parent_id, owner=owner, sort_order=0))
        db.commit()
        return fid
    finally:
        db.close()


def _make_doc(title="t", owner="tester", folder_id=None):
    db = _TS()
    try:
        doc = Document(
            id=str(uuid.uuid4()), title=title, language="markdown",
            current_content="hi", version_count=1, is_active=True,
            owner=owner, folder_id=folder_id,
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


# ---- pure helpers ----


def test_descendant_ids_collects_children_and_grandchildren():
    a = Folder(id="a", name="a", parent_id=None, owner="t")
    b = Folder(id="b", name="b", parent_id="a", owner="t")
    c = Folder(id="c", name="c", parent_id="b", owner="t")
    d = Folder(id="d", name="d", parent_id=None, owner="t")
    assert froutes._descendant_ids([a, b, c, d], "a") == {"b", "c"}
    assert froutes._descendant_ids([a, b, c, d], "d") == set()


def test_build_tree_nests_and_sorts():
    a = Folder(id="a", name="alpha", parent_id=None, owner="t")
    b = Folder(id="b", name="bravo", parent_id=None, owner="t")
    c = Folder(id="c", name="child", parent_id="a", owner="t")
    tree = froutes._build_tree([a, b, c], {"c": 2})
    assert [n["name"] for n in tree] == ["alpha", "bravo"]
    assert [n["name"] for n in tree[0]["children"]] == ["child"]
    assert tree[0]["children"][0]["doc_count"] == 2


def test_clean_name_rejects_blank():
    try:
        froutes._clean_name("   ")
    except HTTPException as e:
        assert e.status_code == 400
    else:
        raise AssertionError("blank name must be rejected")


# ---- folder CRUD ----


async def test_create_and_list_nested_tree():
    create = _endpoint("POST", "/api/folders")
    list_folders = _endpoint("GET", "/api/folders")

    root = await create(_req(), froutes.FolderCreate(name="Root"))
    child = await create(_req(), froutes.FolderCreate(name="Child", parent_id=root["id"]))

    data = await list_folders(_req())
    assert data["unfiled_count"] == 0
    roots = data["folders"]
    assert [f["name"] for f in roots] == ["Root"]
    assert roots[0]["children"][0]["id"] == child["id"]


async def test_create_rejects_sibling_name_collision():
    create = _endpoint("POST", "/api/folders")
    await create(_req(), froutes.FolderCreate(name="Dup"))
    try:
        await create(_req(), froutes.FolderCreate(name="Dup"))
    except HTTPException as e:
        assert e.status_code == 409
    else:
        raise AssertionError("duplicate sibling name must 409")


async def test_rename_updates_name():
    create = _endpoint("POST", "/api/folders")
    rename = _endpoint("PATCH", "/api/folders/{folder_id}")
    fid = (await create(_req(), froutes.FolderCreate(name="Old")))["id"]
    out = await rename(_req(), fid, froutes.FolderRename(name="New"))
    assert out["name"] == "New"


async def test_move_folder_nests_and_cycle_guards():
    create = _endpoint("POST", "/api/folders")
    move = _endpoint("PATCH", "/api/folders/{folder_id}/move")

    a = (await create(_req(), froutes.FolderCreate(name="A")))["id"]
    b = (await create(_req(), froutes.FolderCreate(name="B", parent_id=a)))["id"]
    c = (await create(_req(), froutes.FolderCreate(name="C", parent_id=b)))["id"]

    # Moving a folder into its own descendant must 400.
    try:
        await move(_req(), a, froutes.FolderMove(parent_id=c))
    except HTTPException as e:
        assert e.status_code == 400
    else:
        raise AssertionError("cycle move must 400")

    # Moving into itself must 400.
    try:
        await move(_req(), a, froutes.FolderMove(parent_id=a))
    except HTTPException as e:
        assert e.status_code == 400
    else:
        raise AssertionError("self-move must 400")

    # Moving a child back to root is fine.
    out = await move(_req(), b, froutes.FolderMove(parent_id=None))
    assert out["parent_id"] is None


async def test_delete_empty_folder_returns_ok():
    create = _endpoint("POST", "/api/folders")
    delete = _endpoint("DELETE", "/api/folders/{folder_id}")

    fid = (await create(_req(), froutes.FolderCreate(name="Empty")))["id"]
    res = await delete(_req(), fid)
    assert res == {"ok": True}
    assert _db_get(Folder, fid) is None


async def test_delete_folder_recursively_deletes_docs_and_subfolders():
    create = _endpoint("POST", "/api/folders")
    delete = _endpoint("DELETE", "/api/folders/{folder_id}")
    move_doc = _doc_endpoint("PATCH", "/api/document/{doc_id}")

    top = (await create(_req(), froutes.FolderCreate(name="Top")))["id"]
    child = (await create(_req(), froutes.FolderCreate(name="Child", parent_id=top)))["id"]
    grandchild = (await create(_req(), froutes.FolderCreate(name="Grand", parent_id=child)))["id"]

    doc_in_top = _make_doc(title="in top")
    doc_in_grand = _make_doc(title="in grand")
    outside_doc = _make_doc(title="outside")  # unfiled, must be untouched
    await move_doc(_req(), doc_in_top, DocumentPatch(folder_id=top))
    await move_doc(_req(), doc_in_grand, DocumentPatch(folder_id=grandchild))

    res = await delete(_req(), top)
    assert res == {"ok": True}

    # Every folder in the subtree is gone.
    for fid in (top, child, grandchild):
        assert _db_get(Folder, fid) is None
    # Documents inside the subtree are moved to Trash (inactive + deleted_at).
    for doc_id in (doc_in_top, doc_in_grand):
        d = _db_get(Document, doc_id)
        assert d is not None
        assert d.is_active is False
        assert d.deleted_at is not None
        assert d.folder_id is None
    # A document outside the folder is untouched.
    outside = _db_get(Document, outside_doc)
    assert outside is not None
    assert outside.is_active is True


async def test_delete_folder_with_child_folders_deletes_them():
    create = _endpoint("POST", "/api/folders")
    delete = _endpoint("DELETE", "/api/folders/{folder_id}")

    parent = (await create(_req(), froutes.FolderCreate(name="P")))["id"]
    child = (await create(_req(), froutes.FolderCreate(name="C", parent_id=parent)))["id"]

    res = await delete(_req(), parent)
    assert res == {"ok": True}
    assert _db_get(Folder, parent) is None
    assert _db_get(Folder, child) is None


async def test_owner_isolation():
    create = _endpoint("POST", "/api/folders")
    list_folders = _endpoint("GET", "/api/folders")
    move = _endpoint("PATCH", "/api/folders/{folder_id}/move")

    mine = (await create(_req("alice"), froutes.FolderCreate(name="Alice")))["id"]

    data = await list_folders(_req("bob"))
    assert data["folders"] == []

    try:
        await move(_req("bob"), mine, froutes.FolderMove(parent_id=None))
    except HTTPException as e:
        assert e.status_code == 404
    else:
        raise AssertionError("cross-owner move must 404")


# ---- model + library filter ----


def test_folder_model_and_document_fk_exist():
    assert Folder.__tablename__ == "folders"
    assert Document.folder_id is not None


async def test_doc_patch_moves_and_unfiles():
    move_doc = _doc_endpoint("PATCH", "/api/document/{doc_id}")
    create = _endpoint("POST", "/api/folders")
    fid = (await create(_req(), froutes.FolderCreate(name="Target")))["id"]

    doc_id = _make_doc()
    out = await move_doc(_req(), doc_id, DocumentPatch(folder_id=fid))
    assert out["folder_id"] == fid
    assert out["folder_name"] == "Target"

    out = await move_doc(_req(), doc_id, DocumentPatch(folder_id=""))
    assert out["folder_id"] is None
