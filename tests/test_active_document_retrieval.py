"""The editor document must be reachable by the model — never only by injection.

Regression cover for the failure where the model answered "I cannot see the
active document: it can only arrive through context injection, and this turn
did not include it". Three properties are asserted here:

1. The open document is always announced as a pointer (title/language/size)
   with no body text, so the model always knows *what* is open.
2. `manage_documents action=read` returns the body with no document_id at all,
   resolving to the editor document — the retrieval path the prompt points at.
3. No editor tool silently retargets to "the most recently updated document"
   when the real target is unknown.
"""

import uuid
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
from core.database import Document, Session as DbSession  # noqa: E402
from src.agent_tools import document_tools  # noqa: E402
from src.agent_tools.document_tools import (  # noqa: E402
    EditDocumentTool,
    ManageDocumentTool,
    UpdateDocumentTool,
    set_active_document,
    split_document_id_header,
)
import tempfile  # noqa: E402

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
    monkeypatch.setattr(document_tools, "SessionLocal", _TS, raising=False)
    # document_tools imports SessionLocal inside each tool body from src.database;
    # patch that module attribute too so the real import resolves to the tmp db.
    import src.database as sdb

    monkeypatch.setattr(sdb, "SessionLocal", _TS)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    set_active_document(None)
    yield
    set_active_document(None)


def _session(sid="s", owner="tester"):
    db = _TS()
    try:
        if db.query(DbSession).filter(DbSession.id == sid).first() is None:
            db.add(DbSession(id=sid, owner=owner, name=sid, model="m", endpoint_url="http://x"))
            db.commit()
    finally:
        db.close()


def _doc(title, content, sid="s", owner="tester"):
    doc_id = str(uuid.uuid4())
    db = _TS()
    try:
        db.add(
            Document(
                id=doc_id,
                session_id=sid,
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


# --------------------------------------------------------------------------
# 1. manage_documents read resolves the editor document with no document_id
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_without_id_returns_active_editor_document():
    _session()
    doc_id = _doc("Meeting notes", "line one\nline two")
    set_active_document(doc_id)

    result = await ManageDocumentTool().execute(
        '{"action": "read"}', {"owner": "tester"}
    )

    assert result.get("exit_code") == 0, result
    assert result["document"]["id"] == doc_id
    assert result["document"]["active"] is True
    assert "line one" in result["document"]["content"]
    # Lines are numbered for the editor document so FIND text lines up.
    assert "1\tline one" in result["document"]["content"]
    assert "2\tline two" in result["document"]["content"]


@pytest.mark.asyncio
async def test_read_with_explicit_id_is_not_numbered_and_not_active():
    _session()
    active_id = _doc("Open doc", "open body")
    other_id = _doc("Library doc", "library body")
    set_active_document(active_id)

    result = await ManageDocumentTool().execute(
        '{"action": "read", "document_id": "%s"}' % other_id, {"owner": "tester"}
    )

    assert result["document"]["id"] == other_id
    assert result["document"]["active"] is False
    assert result["document"]["content"] == "library body"


@pytest.mark.asyncio
async def test_read_with_no_active_document_errors_with_both_remedies():
    _session()
    recent_id = _doc("Some doc", "body")

    result = await ManageDocumentTool().execute(
        '{"action": "read"}', {"owner": "tester"}
    )

    assert result["exit_code"] == 1
    assert result.get("no_active_document") is True
    # Exactly two ways in: name the document, or open it in the editor.
    assert "title" in result["error"]
    assert "open it in the editor" in result["error"]
    # The most recent document is explicitly NOT an option — and its id must not
    # be offered, or the model will treat it as a suggested fallback.
    assert recent_id not in result["error"]
    assert "Do NOT fall back" in result["error"]


@pytest.mark.asyncio
async def test_read_by_title():
    _session()
    stem = f"riassunto-{uuid.uuid4().hex[:8]}"
    _doc("Most recent", "other body")
    target_id = _doc(f"{stem}.md", "the meeting body")

    result = await ManageDocumentTool().execute(
        json.dumps({"action": "read", "title": f"{stem}.md"}),
        {"owner": "tester"},
    )

    assert result.get("exit_code") == 0, result
    assert result["document"]["id"] == target_id
    assert "the meeting body" in result["document"]["content"]


@pytest.mark.asyncio
async def test_read_by_path_and_without_extension():
    _session()
    # Unique stem: the test DB is shared across tests in this module.
    stem = f"note-{uuid.uuid4().hex[:8]}"
    target_id = _doc(f"{stem}.md", "body")

    for name in (f"documenti/{stem}.md", stem):
        result = await ManageDocumentTool().execute(
            json.dumps({"action": "read", "title": name}), {"owner": "tester"}
        )
        assert result.get("exit_code") == 0, (name, result)
        assert result["document"]["id"] == target_id


@pytest.mark.asyncio
async def test_read_by_ambiguous_title_lists_candidates():
    _session()
    _doc("Report alpha", "a")
    _doc("Report beta", "b")

    result = await ManageDocumentTool().execute(
        '{"action": "read", "title": "Report"}', {"owner": "tester"}
    )

    assert result["exit_code"] == 1
    assert result.get("ambiguous_title") is True
    assert "Report alpha" in result["error"]
    assert "Report beta" in result["error"]


# --------------------------------------------------------------------------
# 2. No silent retargeting to "the most recently updated document"
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_without_target_does_not_touch_most_recent_document():
    _session()
    recent_id = _doc("Most recent", "hello world")
    set_active_document(None)

    result = await EditDocumentTool().execute(
        "<<<FIND>>>\nhello\n<<<REPLACE>>>\nbye\n<<<END>>>",
        {"owner": "tester"},
    )

    assert result.get("error"), result
    assert result.get("no_active_document") is True

    db = _TS()
    try:
        untouched = db.query(Document).filter(Document.id == recent_id).first()
        assert untouched.current_content == "hello world"
        assert untouched.version_count == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_update_without_target_does_not_rewrite_most_recent_document():
    _session()
    recent_id = _doc("Most recent", "original")
    set_active_document(None)

    result = await UpdateDocumentTool().execute("brand new body", {"owner": "tester"})

    assert result.get("error"), result
    assert result.get("no_active_document") is True

    db = _TS()
    try:
        untouched = db.query(Document).filter(Document.id == recent_id).first()
        assert untouched.current_content == "original"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_edit_honours_explicit_document_id_from_tool_arguments():
    _session()
    other_id = _doc("Most recent", "alpha beta")
    target_id = _doc("Target", "alpha beta", sid="s")
    set_active_document(None)

    content, header_id = split_document_id_header(
        f"<<<DOCUMENT_ID>>>\n{target_id}\n<<<FIND>>>\nalpha\n<<<REPLACE>>>\nomega\n<<<END>>>"
    )
    assert header_id == target_id
    assert content.startswith("<<<FIND>>>")

    result = await EditDocumentTool().execute(
        f"<<<DOCUMENT_ID>>>\n{target_id}\n<<<FIND>>>\nalpha\n<<<REPLACE>>>\nomega\n<<<END>>>",
        {"owner": "tester"},
    )

    assert result.get("exit_code") != 1, result
    db = _TS()
    try:
        assert db.query(Document).filter(Document.id == target_id).first().current_content == "omega beta"
        assert db.query(Document).filter(Document.id == other_id).first().current_content == "alpha beta"
    finally:
        db.close()


def test_split_document_id_header_is_optional():
    assert split_document_id_header("<<<FIND>>>\nx\n<<<END>>>") == (
        "<<<FIND>>>\nx\n<<<END>>>",
        None,
    )


@pytest.mark.asyncio
async def test_read_error_distinguishes_preview_from_stale_editor():
    _session()

    preview = await ManageDocumentTool().execute(
        '{"action": "read"}',
        {"owner": "tester", "library_preview_doc_id": "some-preview-id"},
    )
    assert preview["exit_code"] == 1
    assert "PREVIEW" in preview["error"]
    assert "Open in editor" in preview["error"]
    # A preview must NOT be reported as if the editor were closed.
    assert "No document is open in the editor" not in preview["error"]

    stale = await ManageDocumentTool().execute(
        '{"action": "read"}',
        {"owner": "tester", "editor_open": True, "editor_stale": True},
    )
    assert stale["exit_code"] == 1
    assert "stale" in stale["error"].lower()
    assert "not the editor" not in stale["error"]


@pytest.mark.asyncio
async def test_preview_id_is_not_promoted_to_active_by_read():
    # The preview is read-only: with a preview id and no active pointer, a plain
    # read must not silently read the previewed document.
    _session()
    previewed = _doc("Previewed doc", "preview body")
    set_active_document(None)

    result = await ManageDocumentTool().execute(
        '{"action": "read"}',
        {"owner": "tester", "library_preview_doc_id": previewed},
    )
    assert result["exit_code"] == 1
    assert result.get("no_active_document") is True


# --------------------------------------------------------------------------
# 3. Document access tools are offered on every agent turn
# --------------------------------------------------------------------------


def test_title_lookup_escapes_like_wildcards():
    # A title containing % must not widen the LIKE scan into a substring match.
    source = (Path(__file__).resolve().parents[1] / "src/agent_tools/document_tools.py").read_text()
    body = source[source.index("def _find_documents_by_title"):]
    body = body[: body.index("def _resolve_document_target")]
    assert 'replace("%", "\\\\%")' in body
    assert 'escape="\\\\"' in body





def test_document_access_tools_are_always_available():
    from src.agent_loop import _DOCUMENT_ACCESS_TOOLS

    assert _DOCUMENT_ACCESS_TOOLS == {
        "manage_documents",
        "edit_document",
        "update_document",
        "suggest_document",
    }


def test_manage_documents_schema_exposes_read():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    schemas = {s["function"]["name"]: s["function"] for s in FUNCTION_TOOL_SCHEMAS}
    action = schemas["manage_documents"]["parameters"]["properties"]["action"]
    assert "read" in action["enum"]
    # document_id must be optional so "read the open document" needs no id.
    assert "document_id" not in schemas["manage_documents"]["parameters"]["required"]


# --------------------------------------------------------------------------
# 4. The pointer is emitted on every turn, and carries no body text
# --------------------------------------------------------------------------


def _prompt_messages(active_document, user_text):
    from src.agent_loop import _build_system_prompt

    messages, _schemas = _build_system_prompt(
        [{"role": "user", "content": user_text}],
        "test-model",
        active_document,
        None,
        owner="tester",
    )
    return messages


class _FakeDoc:
    def __init__(self, doc_id, title, content, language="markdown"):
        self.id = doc_id
        self.title = title
        self.current_content = content
        self.language = language


def test_pointer_message_present_for_unrelated_turn_without_body():
    doc = _FakeDoc("doc-1", "Meeting notes", "SECRET_BODY_TEXT")
    messages = _prompt_messages(doc, "who am i?")

    pointers = [
        m for m in messages
        if isinstance(m.get("content"), str)
        and "ACTIVE EDITOR DOCUMENT" in m["content"]
    ]
    assert pointers, "the open document must always be announced"
    text = pointers[0]["content"]
    assert "Meeting notes" in text
    assert "manage_documents" in text
    # No body text: the model fetches it with action=read.
    assert "SECRET_BODY_TEXT" not in text


def test_pointer_absent_when_no_document_open():
    messages = _prompt_messages(None, "who am i?")
    assert not any(
        isinstance(m.get("content"), str) and "ACTIVE EDITOR DOCUMENT" in m["content"]
        for m in messages
    )


# --------------------------------------------------------------------------
# 5. The tool-facing pointer survives prompt rebuilding
# --------------------------------------------------------------------------


def test_build_system_prompt_does_not_touch_the_tool_pointer():
    """Prompt assembly must not own `_active_document_id`.

    `_build_system_prompt` runs once per candidate/retry and receives
    `_prompt_active_document`, which is None on every non-selection turn. When it
    wrote (and cleared) the pointer, a later rebuild nulled it between prompt
    assembly and tool execution: the prompt announced the document, then
    `manage_documents read` replied "no document is open".
    """
    import inspect

    from src.agent_loop import _build_system_prompt

    source = inspect.getsource(_build_system_prompt)
    calls = [
        line.strip()
        for line in source.splitlines()
        if "set_active_document(" in line and not line.strip().startswith("#")
    ]
    assert not calls, (
        "_build_system_prompt must not call set_active_document (it owns no "
        f"tool state); found: {calls}"
    )


def test_pointer_is_set_by_the_agent_loop_for_the_turn():
    import inspect

    from src.agent_loop import stream_agent_loop

    source = inspect.getsource(stream_agent_loop)
    assert "set_active_document(" in source
    # It must be set from the request's own document, not from the prompt gate.
    assert "_prompt_active_document = active_document if _active_document_relevant else None" in source
    set_at = source.index("set_active_document(str(_pointer_id)")
    gate_at = source.index("_prompt_active_document = active_document if _active_document_relevant")
    assert gate_at < set_at, "pointer must be set after the prompt gate is computed"


@pytest.mark.asyncio
async def test_tool_pointer_survives_prompt_rebuilds_without_full_text():
    """End-to-end shape of the bug: build the prompt twice, then run the tool."""
    _session()
    doc_id = _doc("Editor doc", "alpha\nbeta")
    set_active_document(doc_id)

    # A non-selection turn: the prompt gate is False, so the builder is passed
    # no document at all on the follow-up (candidate) rebuild.
    _prompt_messages(None, "who am i?")
    assert document_tools.get_active_document() == doc_id, (
        "a prompt rebuild without a document must not clear the tool pointer"
    )

    result = await ManageDocumentTool().execute('{"action": "read"}', {"owner": "tester"})
    assert result.get("exit_code") == 0, result
    assert result["document"]["id"] == doc_id
