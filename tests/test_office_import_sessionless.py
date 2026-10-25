"""Session-less Office imports must not violate the documents.session_id FK.

The bug this locks down: the Library's `POST /api/documents/import-office`
passed `session_id=""` for a session-less import. `documents.session_id` is a
foreign key to `sessions.id`, and an empty string is NOT NULL, so SQLite tried to
satisfy it against a session literally named "" and every docx upload failed with
"FOREIGN KEY constraint failed" — while chat attachments (which always have a
real session) worked fine.
"""

import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
from core.database import Document, Session as DbSession  # noqa: E402
from src.office_doc import create_office_document  # noqa: E402

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    """Point every DB entry point at the temp database."""
    import src.database as sdb

    monkeypatch.setattr(sdb, "SessionLocal", _TS)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    # create_office_document sets the in-memory tool pointer; harmless here but
    # keep it from leaking between tests.
    import src.agent_tools.document_tools as dt

    monkeypatch.setattr(dt, "set_active_document", lambda *_a, **_k: None)
    yield


def _row(doc_id):
    db = _TS()
    try:
        return db.query(Document).filter(Document.id == doc_id).first()
    finally:
        db.close()


def _make_session(sid="sess-1", owner="tester"):
    db = _TS()
    try:
        db.add(DbSession(id=sid, owner=owner, name=sid, model="m", endpoint_url="http://x"))
        db.commit()
    finally:
        db.close()


# ── the regression ─────────────────────────────────────────────────────────

def test_empty_session_id_is_stored_as_null():
    """`session_id=""` used to raise FOREIGN KEY constraint failed."""
    doc_id = create_office_document(
        session_id="",
        upload_id="up-1",
        title="Silvestri_261022",
        body_text="Body.\n\n## Comments (1)\n",
        comment_count=1,
    )
    assert doc_id, "import returned no document id"

    row = _row(doc_id)
    assert row is not None
    # NULL, not the empty string — that is what makes it a valid library doc.
    assert row.session_id is None


def test_none_session_id_works_too():
    doc_id = create_office_document(
        session_id=None,
        upload_id="up-2",
        title="Library doc",
        body_text="Body.",
    )
    assert doc_id
    assert _row(doc_id).session_id is None


def test_real_session_id_is_still_preserved():
    """The fix must not over-normalise: a real session must still be linked."""
    _make_session("sess-keep", owner="tester")
    doc_id = create_office_document(
        session_id="sess-keep",
        upload_id="up-3",
        title="Chat doc",
        body_text="Body.",
    )
    assert doc_id
    assert _row(doc_id).session_id == "sess-keep"


def test_version_summary_records_the_comment_count():
    doc_id = create_office_document(
        session_id="",
        upload_id="up-4",
        title="Commented",
        body_text="Body.",
        comment_count=7,
    )
    db = _TS()
    try:
        from core.database import DocumentVersion

        ver = (
            db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == doc_id)
            .first()
        )
        assert "7 comments" in (ver.summary or "")
    finally:
        db.close()


# ── route wiring ───────────────────────────────────────────────────────────

def test_route_passes_none_not_empty_string_for_library_imports():
    """The route is where the empty string came from; keep the `or None`."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "routes/document/document_routes.py"
    ).read_text(encoding="utf-8")
    start = src.index('    @router.post("/api/documents/import-office")')
    end = src.index("# ---- GET /api/documents/library ----", start)
    route = src[start:end]

    assert "session_id=session_id or None" in route
    assert 'session_id=session_id or ""' not in route


def test_chat_processor_also_normalises():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "src/document_processor.py"
    ).read_text(encoding="utf-8")
    assert "session_id=session_id or None" in src


def test_import_office_route_is_registered():
    from unittest.mock import MagicMock

    import routes.document_routes as droutes

    router = droutes.setup_document_routes(MagicMock(), None)
    paths = {r.path for r in router.routes}
    assert "/api/documents/import-office" in paths
