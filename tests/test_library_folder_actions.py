"""Folder-aware library writes and their human-approval boundary.

Covers the LLM-facing side of folder placement:

* ``make_library_file`` can create directly in a folder (id, path, or unique
  name) while keeping Unfiled as the default.
* ``manage_documents action=move`` can move a document between folders.
* ``library_action_approval_review`` only raises the human-approval card for
  actions that change a file's folder.
* Library approvals are one exact action: they do not require the external
  untrusted-context taint state and they never widen into task/chat scope.
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
import src.agent_tools.document_tools as dt
import src.agent_tools.library_tools as lt
from core.database import Document, Folder
from src.tool_approvals import (
    LIBRARY_FOLDER_APPROVAL_CATEGORY,
    ToolApprovalStore,
)
from src.tool_approval_scopes import ToolApprovalScope
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action


@pytest.fixture(autouse=True)
def _bind(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    cdb.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(lt, "SessionLocal", session_factory)
    monkeypatch.setattr(cdb, "SessionLocal", session_factory)
    yield


def _folder(name, owner="tester", parent_id=None):
    fid = str(uuid.uuid4())
    db = cdb.SessionLocal()
    try:
        db.add(Folder(id=fid, name=name, parent_id=parent_id, owner=owner, sort_order=0))
        db.commit()
        return fid
    finally:
        db.close()


def _doc(title, owner="tester", folder_id=None, content="body"):
    db = cdb.SessionLocal()
    try:
        doc_id = str(uuid.uuid4())
        db.add(
            Document(
                id=doc_id,
                title=title,
                language="markdown",
                current_content=content,
                version_count=1,
                is_active=True,
                owner=owner,
                folder_id=folder_id,
            )
        )
        db.commit()
        return doc_id
    finally:
        db.close()


def _row(doc_id):
    db = cdb.SessionLocal()
    try:
        return db.query(Document).filter(Document.id == doc_id).first()
    finally:
        db.close()


def _make(args, owner="tester"):
    return lt.MakeLibraryFileTool().execute(json.dumps(args), {"owner": owner})


def _manage(args, owner="tester"):
    return dt.ManageDocumentTool().execute(json.dumps(args), {"owner": owner})


# ── make_library_file ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_make_library_file_creates_in_folder_by_name():
    work = _folder("Work")
    out = await _make({"name": "notes.md", "content": "hello", "folder": "Work"})
    assert out["exit_code"] == 0, out
    doc = _row(out["document"]["id"])
    assert doc.folder_id == work
    assert out["document"]["folder"] == "Work"
    assert "Work" in out["output"]


@pytest.mark.asyncio
async def test_make_library_file_creates_in_nested_path():
    work = _folder("Work")
    reports = _folder("Reports", parent_id=work)
    out = await _make(
        {"name": "q1.md", "content": "q1", "folder": "Work/Reports"}
    )
    assert out["exit_code"] == 0, out
    assert _row(out["document"]["id"]).folder_id == reports


@pytest.mark.asyncio
async def test_make_library_file_accepts_folder_id():
    work = _folder("Work")
    out = await _make(
        {"name": "notes.md", "content": "hello", "folder_id": work}
    )
    assert out["exit_code"] == 0, out
    assert _row(out["document"]["id"]).folder_id == work


@pytest.mark.asyncio
async def test_make_library_file_defaults_to_unfiled():
    out = await _make({"name": "loose.md", "content": "loose"})
    assert out["exit_code"] == 0, out
    assert _row(out["document"]["id"]).folder_id is None
    assert "Unfiled" in out["output"]


@pytest.mark.asyncio
async def test_make_library_file_unknown_folder_errors():
    out = await _make({"name": "notes.md", "content": "hello", "folder": "Nope"})
    assert out["exit_code"] == 1
    assert "not found" in out["error"]


@pytest.mark.asyncio
async def test_make_library_file_rejects_ambiguous_bare_name():
    work = _folder("Work")
    _folder("Work", parent_id=work)
    out = await _make({"name": "notes.md", "content": "hello", "folder": "Work"})
    assert out["exit_code"] == 1
    assert out.get("ambiguous_folder") is True
    assert "full path" in out["error"]


@pytest.mark.asyncio
async def test_make_library_file_owner_isolation():
    _folder("Private", owner="alice")
    out = await _make(
        {"name": "notes.md", "content": "hello", "folder": "Private"},
        owner="bob",
    )
    assert out["exit_code"] == 1
    assert "not found" in out["error"]


# ── manage_documents move ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manage_documents_moves_document_to_folder():
    work = _folder("Work")
    doc_id = _doc("notes.md")
    out = await _manage(
        {"action": "move", "document_id": doc_id, "folder": "Work"}
    )
    assert out["exit_code"] == 0, out
    assert _row(doc_id).folder_id == work
    assert out["document"]["folder"] == "Work"
    assert "Work" in out["response"]


@pytest.mark.asyncio
async def test_manage_documents_moves_document_to_unfiled():
    work = _folder("Work")
    doc_id = _doc("notes.md", folder_id=work)
    out = await _manage(
        {"action": "move", "document_id": doc_id, "folder": "unfiled"}
    )
    assert out["exit_code"] == 0, out
    assert _row(doc_id).folder_id is None


@pytest.mark.asyncio
async def test_manage_documents_move_accepts_title():
    work = _folder("Work")
    doc_id = _doc("notes.md")
    out = await _manage({"action": "move", "title": "notes.md", "folder": "Work"})
    assert out["exit_code"] == 0, out
    assert _row(doc_id).folder_id == work


@pytest.mark.asyncio
async def test_manage_documents_move_requires_destination():
    doc_id = _doc("notes.md")
    out = await _manage({"action": "move", "document_id": doc_id})
    assert out["exit_code"] == 1
    assert "destination" in out["error"]


@pytest.mark.asyncio
async def test_manage_documents_move_unknown_folder_errors():
    doc_id = _doc("notes.md")
    out = await _manage(
        {"action": "move", "document_id": doc_id, "folder": "Nope"}
    )
    assert out["exit_code"] == 1
    assert "not found" in out["error"]


@pytest.mark.asyncio
async def test_manage_documents_move_owner_isolation():
    _folder("Private", owner="alice")
    doc_id = _doc("notes.md", owner="alice")
    out = await _manage(
        {"action": "move", "document_id": doc_id, "folder": "Private"},
        owner="bob",
    )
    assert out["exit_code"] == 1


# ── approval review ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_create_in_folder_names_destination():
    _folder("Work")
    review = lt.library_action_approval_review(
        "make_library_file",
        json.dumps({"name": "notes.md", "content": "hello", "folder": "Work"}),
        "tester",
    )
    assert review is not None
    assert review["category"] == LIBRARY_FOLDER_APPROVAL_CATEGORY
    assert 'create "notes.md"' in review["question"]
    assert '"Work"' in review["question"]
    assert "hello" not in review["display_content"]


@pytest.mark.asyncio
async def test_review_create_in_unfiled_does_not_ask():
    review = lt.library_action_approval_review(
        "make_library_file",
        json.dumps({"name": "notes.md", "content": "hello"}),
        "tester",
    )
    assert review is None


@pytest.mark.asyncio
async def test_review_move_names_destination():
    work = _folder("Work")
    doc_id = _doc("notes.md")
    review = lt.library_action_approval_review(
        "manage_documents",
        json.dumps({"action": "move", "document_id": doc_id, "folder": "Work"}),
        "tester",
    )
    assert review is not None
    assert review["category"] == LIBRARY_FOLDER_APPROVAL_CATEGORY
    assert 'move "notes.md"' in review["question"]
    assert '"Work"' in review["question"]
    assert work


# ── approval store / dispatcher ────────────────────────────────────────────


def _library_pending(store, content, **overrides):
    values = {
        "owner": "tester",
        "session_id": "session-1",
        "origin_run_id": "run-1",
        "tool_name": "make_library_file",
        "content": content,
        "workspace": None,
        "external_untrusted_context_seen": False,
        "capabilities": capabilities_for_action("make_library_file", content),
        "approval_category": LIBRARY_FOLDER_APPROVAL_CATEGORY,
        "question": 'Do you want to create "notes.md" in "Work"?',
        "display_content": 'create "notes.md" (5 chars) in "Work"',
    }
    values.update(overrides)
    return store.create(**values)


def test_library_approval_card_is_allow_once_or_deny():
    store = ToolApprovalStore()
    content = json.dumps({"name": "notes.md", "content": "hello", "folder": "Work"})
    pending = _library_pending(store, content)
    payload = pending.public_payload()

    assert payload["question"] == 'Do you want to create "notes.md" in "Work"?'
    assert [opt["label"] for opt in payload["options"]] == ["Allow once", "Deny"]
    assert payload["action"]["content"] == 'create "notes.md" (5 chars) in "Work"'


def test_library_approval_consumes_as_single_action():
    store = ToolApprovalStore()
    content = json.dumps({"name": "notes.md", "content": "hello", "folder": "Work"})
    pending = _library_pending(store, content)
    grant = store.consume(
        pending.approval_id,
        decision="approve_task",
        owner="tester",
        session_id="session-1",
    )
    assert grant is not None
    assert grant.scope is ToolApprovalScope.SINGLE_ACTION
    assert grant.allow_remaining_actions is False
    assert grant.claim(
        owner="tester",
        session_id="session-1",
        tool_name="make_library_file",
        content=content,
        workspace=None,
    )


@pytest.mark.asyncio
async def test_agent_loop_pauses_for_folder_create_approval(monkeypatch):
    _folder("Work")
    import src.agent_loop as agent_loop
    from src.tool_approvals import tool_approval_store

    monkeypatch.setattr(
        agent_loop, "get_setting", lambda key, default=None: default, raising=False
    )
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(
        agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False
    )

    async def fake_stream(*args, **kwargs):
        block = (
            "```make_library_file\n"
            '{"name":"notes.md","content":"hello","folder":"Work"}\n'
            "```"
        )
        yield "data: " + json.dumps({"delta": block}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def should_not_execute(*args, **kwargs):
        raise AssertionError("library folder action executed without approval")

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", should_not_execute)

    events = []
    async for chunk in agent_loop.stream_agent_loop(
        "http://local.test/v1",
        "small-local-model",
        [{"role": "user", "content": "save this in Work"}],
        max_rounds=1,
        owner="tester",
        relevant_tools={"make_library_file", "list_library_files"},
    ):
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            events.append(json.loads(chunk[6:]))

    approvals = [
        event.get("ask_user")
        for event in events
        if event.get("type") == "tool_output" and event.get("ask_user")
    ]
    assert approvals, events
    payload = approvals[0]
    assert payload["kind"] == "tool_approval"
    assert 'create "notes.md"' in payload["question"]
    assert '"Work"' in payload["question"]
    assert [opt["label"] for opt in payload["options"]] == ["Allow once", "Deny"]

    try:
        tool_approval_store.consume(
            payload["approval_id"],
            decision="deny",
            owner="tester",
            session_id=None,
        )
    except Exception:
        pass


@pytest.mark.asyncio
async def test_approved_folder_create_executes_the_sealed_action(monkeypatch):
    work = _folder("Work")
    import src.agent_loop as agent_loop
    from src.tool_approvals import tool_approval_store

    monkeypatch.setattr(
        agent_loop, "get_setting", lambda key, default=None: default, raising=False
    )
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(
        agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False
    )

    responses = [
        (
            "```make_library_file\n"
            '{"name":"notes.md","content":"hello","folder":"Work"}\n'
            "```"
        ),
        "Done.",
    ]

    async def fake_stream(*args, **kwargs):
        yield "data: " + json.dumps({"delta": responses.pop(0)}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    async def run(**kwargs):
        events = []
        async for chunk in agent_loop.stream_agent_loop(
            "http://local.test/v1",
            "small-local-model",
            [{"role": "user", "content": "save this in Work"}],
            owner="tester",
            relevant_tools={"make_library_file", "list_library_files"},
            **kwargs,
        ):
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                events.append(json.loads(chunk[6:]))
        return events

    first = await run(max_rounds=1)
    approval = next(
        event["ask_user"]
        for event in first
        if event.get("type") == "tool_output" and event.get("ask_user")
    )
    db = cdb.SessionLocal()
    try:
        assert db.query(Document).count() == 0
    finally:
        db.close()

    grant = tool_approval_store.consume(
        approval["approval_id"],
        decision="approve_task",
        owner="tester",
        session_id=None,
    )
    assert grant is not None
    await run(max_rounds=1, exact_approval=grant)

    db = cdb.SessionLocal()
    try:
        doc = db.query(Document).filter(Document.title == "notes.md").first()
        assert doc is not None
        assert doc.folder_id == work
    finally:
        db.close()


@pytest.mark.asyncio
async def test_library_approval_executes_without_external_taint(monkeypatch):
    import src.tool_execution as tool_execution
    from collections import namedtuple

    ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])
    content = json.dumps({"name": "notes.md", "content": "hello", "folder": "Work"})
    store = ToolApprovalStore()
    pending = _library_pending(store, content)
    grant = store.consume(
        pending.approval_id,
        decision="approve_task",
        owner="tester",
        session_id="session-1",
    )

    calls = []

    async def fake_implementation(block, **kwargs):
        calls.append((block.tool_type, block.content))
        return "make_library_file", {"output": "ok", "exit_code": 0}

    monkeypatch.setattr(
        tool_execution, "_execute_tool_block_impl", fake_implementation
    )
    _, result = await tool_execution.execute_tool_block(
        ToolBlock("make_library_file", content),
        session_id="session-1",
        owner="tester",
        workspace=None,
        security_context=ToolRunSecurityContext(
            external_untrusted_context_seen=False
        ),
        exact_approval=grant,
    )

    assert result["exit_code"] == 0
    assert calls == [("make_library_file", content)]
