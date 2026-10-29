"""Target-aware editor policy: the open document is suggestion-only.

Direct `edit_document` / `update_document` calls are allowed for documents that
are not open in the editor, but the document the browser currently shows can
only be changed through `suggest_document`. These tests exercise the real tool
implementations against a temp SQLite DB.
"""
import json
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
from core.database import Document  # noqa: E402
import src.database as sdb  # noqa: E402
from src.agent_tools import document_tools as dt  # noqa: E402
from src.agent_tools.document_tools import (  # noqa: E402
    EditDocumentTool,
    ManageDocumentTool,
    SuggestDocumentTool,
    UpdateDocumentTool,
    set_active_document,
)

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
    monkeypatch.setattr(dt, "SessionLocal", _TS, raising=False)
    monkeypatch.setattr(sdb, "SessionLocal", _TS)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    set_active_document(None)
    yield
    set_active_document(None)


def _doc(title, content, owner="tester"):
    doc_id = str(uuid.uuid4())
    db = _TS()
    try:
        db.add(
            Document(
                id=doc_id,
                session_id=None,
                title=title,
                language="markdown",
                current_content=content,
                version_count=1,
                is_active=True,
                owner=owner,
            )
        )
        db.commit()
    finally:
        db.close()
    return doc_id


def _get(doc_id):
    db = _TS()
    try:
        return db.query(Document).filter(Document.id == doc_id).first()
    finally:
        db.close()


def _ctx(editor_open=True, **extra):
    return {"owner": "tester", "editor_open": editor_open, **extra}


def _edit_block(find, replace):
    return f"<<<FIND>>>\n{find}\n<<<REPLACE>>>\n{replace}\n<<<END>>>"


@pytest.mark.asyncio
async def test_update_open_document_is_blocked():
    doc_id = _doc("Open doc", "original")
    set_active_document(doc_id)

    result = await UpdateDocumentTool().execute("rewritten", _ctx())

    assert result.get("policy") == "editor_suggest_only"
    assert result.get("exit_code") == 1
    assert "suggest_document" in result["error"]
    row = _get(doc_id)
    assert row.current_content == "original"
    assert row.version_count == 1


@pytest.mark.asyncio
async def test_edit_open_document_is_blocked():
    doc_id = _doc("Open doc", "hello world")
    set_active_document(doc_id)

    result = await EditDocumentTool().execute(
        _edit_block("hello", "hi"), _ctx()
    )

    assert result.get("policy") == "editor_suggest_only"
    row = _get(doc_id)
    assert row.current_content == "hello world"
    assert row.version_count == 1


@pytest.mark.asyncio
async def test_explicit_other_document_still_editable_while_editor_open():
    open_id = _doc("Open doc", "open body")
    other_id = _doc("Other doc", "other body")
    set_active_document(open_id)

    result = await UpdateDocumentTool().execute(
        f"<<<DOCUMENT_ID>>>\n{other_id}\nnew other body", _ctx()
    )

    assert result.get("action") == "update", result
    assert result["doc_id"] == other_id
    assert _get(other_id).current_content == "new other body"
    assert _get(open_id).current_content == "open body"


@pytest.mark.asyncio
async def test_direct_edit_is_allowed_when_editor_is_closed():
    doc_id = _doc("Closed doc", "hello world")
    set_active_document(doc_id)

    result = await EditDocumentTool().execute(
        _edit_block("hello", "hi"), _ctx(editor_open=False)
    )

    assert result.get("action") == "edit", result
    assert _get(doc_id).current_content == "hi world"


@pytest.mark.asyncio
async def test_suggest_document_still_works_on_the_open_document():
    doc_id = _doc("Open doc", "The budget must be approved by Friday.")
    set_active_document(doc_id)

    result = await SuggestDocumentTool().execute(
        _suggest_block(
            "The budget must be approved by Friday.",
            "The budget must be signed off by Friday.",
        ),
        _ctx(),
    )

    assert result.get("action") == "suggest", result
    assert result["count"] == 1
    assert _get(doc_id).current_content == "The budget must be approved by Friday."


@pytest.mark.asyncio
async def test_deleting_the_open_document_is_blocked():
    doc_id = _doc("Open doc", "body")
    other_id = _doc("Other doc", "other")
    set_active_document(doc_id)

    blocked = await ManageDocumentTool().execute(
        json.dumps({"action": "delete", "document_id": doc_id}), _ctx()
    )
    assert blocked.get("policy") == "editor_suggest_only"
    assert _get(doc_id).is_active is True

    allowed = await ManageDocumentTool().execute(
        json.dumps({"action": "delete", "document_id": other_id}), _ctx()
    )
    assert allowed.get("exit_code") == 0
    assert _get(other_id).is_active is False


@pytest.mark.asyncio
async def test_email_compose_draft_keeps_direct_update():
    content = "To: a@example.com\nSubject: Hi\n---\nbody"
    doc_id = _doc("New email", content)
    set_active_document(doc_id)

    result = await UpdateDocumentTool().execute(
        "To: a@example.com\nSubject: Hi\n---\nnew body", _ctx()
    )

    assert result.get("action") == "update", result
    assert "new body" in _get(doc_id).current_content


@pytest.mark.asyncio
async def test_app_api_cannot_bypass_document_writes():
    """app_api can reach UI routes; it must not become a document-write bypass."""
    from src.tools.system import do_app_api

    result = await do_app_api(
        json.dumps({
            "action": "call",
            "method": "PUT",
            "path": "/api/document/abc",
            "body": {"content": "bypass"},
        }),
        owner="tester",
    )
    assert result["exit_code"] == 1
    assert "Don't mutate documents via app_api" in result["error"]


def test_schema_and_document_rules_state_the_policy():
    from src.agent_loop import _DOMAIN_RULES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    schemas = {s["function"]["name"]: s["function"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "can only be changed through suggest_document" in schemas["edit_document"]["description"]
    assert "can only be changed through suggest_document" in schemas["update_document"]["description"]
    assert "required path" in schemas["suggest_document"]["description"]
    rules = _DOMAIN_RULES["documents"]
    assert "ONLY be changed through `suggest_document`" in rules
    assert "email compose draft" in rules


def _suggest_block(find, replace):
    return (
        f"<<<FIND>>>\n{find}\n<<<SUGGEST>>>\n{replace}"
        f"\n<<<REASON>>>\nmore precise wording\n<<<END>>>"
    )


def test_approved_tool_path_forwards_editor_state():
    """The approval-continuation path must pass editor_open too, or a sealed
    direct edit could bypass the target-aware guard."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "src/agent_loop.py").read_text(encoding="utf-8")
    approved = src[src.index("async def _run_approved_tool"):]
    approved = approved[:approved.index("finally:")]
    assert "editor_open=editor_has_document" in approved
    assert "editor_stale=editor_stale" in approved


def test_teacher_escalation_forwards_editor_state():
    """The teacher runs in the same chat; without editor_open it could bypass
    the suggestion-only guard for the open document."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    loop_src = (root / "src/agent_loop.py").read_text(encoding="utf-8")
    assert "editor_has_document=editor_has_document," in loop_src
    teacher_src = (root / "src/teacher_escalation.py").read_text(encoding="utf-8")
    assert "editor_has_document: bool = False" in teacher_src
    assert "editor_has_document=editor_has_document," in teacher_src
