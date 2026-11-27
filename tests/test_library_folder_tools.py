"""LLM folder create/move/rename tool and its human-approval boundary.

The tool shares its resolvers with ``library_action_approval_review`` so the
approval card always names the exact folder operation that will execute.
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
import src.agent_tools.folder_tools as ft
import src.agent_tools.library_tools as lt
from core.database import Folder
from src.tool_approvals import (
    LIBRARY_FOLDER_APPROVAL_CATEGORY,
    tool_approval_store,
)
from src.tool_approval_scopes import ToolApprovalScope


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
    monkeypatch.setattr(ft, "SessionLocal", session_factory)
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


def _row(folder_id):
    db = cdb.SessionLocal()
    try:
        return db.query(Folder).filter(Folder.id == folder_id).first()
    finally:
        db.close()


def _path(folder_id, owner="tester"):
    db = cdb.SessionLocal()
    try:
        return lt.folder_path(db, owner, _row(folder_id))
    finally:
        db.close()


def _call(args, owner="tester"):
    return ft.ManageLibraryFoldersTool().execute(json.dumps(args), {"owner": owner})


# ── create ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_folder_at_root():
    out = await _call({"action": "create", "name": "Work"})
    assert out["exit_code"] == 0, out
    folder = _row(out["folder"]["id"])
    assert folder.name == "Work"
    assert folder.parent_id is None
    assert folder.owner == "tester"
    assert out["folder"]["path"] == "Work"
    assert "Work" in out["output"]


@pytest.mark.asyncio
async def test_create_folder_nested_by_name_and_path():
    work = _folder("Work")
    out = await _call({"action": "create", "name": "Reports", "parent": "Work"})
    assert out["exit_code"] == 0, out
    assert _row(out["folder"]["id"]).parent_id == work
    assert out["folder"]["path"] == "Work/Reports"

    out2 = await _call(
        {"action": "create", "name": "2026", "parent": "Work/Reports"}
    )
    assert out2["exit_code"] == 0, out2
    assert out2["folder"]["path"] == "Work/Reports/2026"


@pytest.mark.asyncio
async def test_create_folder_nested_by_id():
    work = _folder("Work")
    out = await _call({"action": "create", "name": "Reports", "parent_id": work})
    assert out["exit_code"] == 0, out
    assert _row(out["folder"]["id"]).parent_id == work


@pytest.mark.asyncio
async def test_create_folder_rejects_duplicate_sibling():
    _folder("Work")
    out = await _call({"action": "create", "name": "Work"})
    assert out["exit_code"] == 1
    assert "already exists" in out["error"]


@pytest.mark.asyncio
async def test_create_folder_rejects_missing_parent():
    out = await _call({"action": "create", "name": "Reports", "parent": "Nope"})
    assert out["exit_code"] == 1
    assert "not found" in out["error"]


@pytest.mark.asyncio
async def test_create_folder_owner_isolation():
    _folder("Private", owner="alice")
    out = await _call(
        {"action": "create", "name": "Reports", "parent": "Private"},
        owner="bob",
    )
    assert out["exit_code"] == 1
    assert "not found" in out["error"]


# ── move ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_move_folder_to_root_and_back():
    work = _folder("Work")
    reports = _folder("Reports", parent_id=work)
    out = await _call(
        {"action": "move", "folder": "Work/Reports", "parent": "unfiled"}
    )
    assert out["exit_code"] == 0, out
    assert _row(reports).parent_id is None
    assert out["folder"]["path"] == "Reports"

    out2 = await _call(
        {"action": "move", "folder": "Reports", "parent": "Work"}
    )
    assert out2["exit_code"] == 0, out2
    assert _row(reports).parent_id == work
    assert out2["folder"]["path"] == "Work/Reports"


@pytest.mark.asyncio
async def test_move_folder_requires_destination():
    _folder("Work")
    out = await _call({"action": "move", "folder": "Work"})
    assert out["exit_code"] == 1
    assert "destination" in out["error"]


@pytest.mark.asyncio
async def test_move_folder_rejects_cycle():
    work = _folder("Work")
    child = _folder("Child", parent_id=work)
    out = await _call(
        {"action": "move", "folder": "Work", "parent": "Work/Child"}
    )
    assert out["exit_code"] == 1
    assert "own subfolder" in out["error"]
    assert _row(work).parent_id is None
    assert _row(child).parent_id == work


@pytest.mark.asyncio
async def test_move_folder_rejects_duplicate_sibling():
    work = _folder("Work")
    archive = _folder("Archive")
    _folder("Reports", parent_id=work)
    other = _folder("Reports", parent_id=archive)
    # Work/Reports and Archive/Reports both exist; moving the first into
    # Archive would collide with the second.
    out = await _call(
        {"action": "move", "folder": "Work/Reports", "parent": "Archive"}
    )
    assert out["exit_code"] == 1
    assert "already exists" in out["error"]
    assert _row(other).parent_id == archive


# ── rename ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_folder_and_nested_path():
    work = _folder("Work")
    reports = _folder("Reports", parent_id=work)
    out = await _call(
        {"action": "rename", "folder": "Work/Reports", "name": "Quarterly"}
    )
    assert out["exit_code"] == 0, out
    assert _row(reports).name == "Quarterly"
    assert out["folder"]["path"] == "Work/Quarterly"


@pytest.mark.asyncio
async def test_rename_folder_rejects_duplicate_sibling():
    work = _folder("Work")
    _folder("Reports", parent_id=work)
    other = _folder("Archive", parent_id=work)
    out = await _call(
        {"action": "rename", "folder": other, "name": "Reports"}
    )
    assert out["exit_code"] == 1
    assert "already exists" in out["error"]


@pytest.mark.asyncio
async def test_rename_folder_requires_name():
    _folder("Work")
    out = await _call({"action": "rename", "folder": "Work"})
    assert out["exit_code"] == 1
    assert "name" in out["error"]


# ── approval review ────────────────────────────────────────────────────────


def _review(args, owner="tester"):
    return lt.library_action_approval_review(
        "manage_library_folders", json.dumps(args), owner
    )


@pytest.mark.asyncio
async def test_review_create_folder_names_destination():
    _folder("Work")
    review = _review({"action": "create", "name": "Reports", "parent": "Work"})
    assert review is not None
    assert review["category"] == LIBRARY_FOLDER_APPROVAL_CATEGORY
    assert review["question"] == (
        'Do you want to create folder "Reports" in "Work"?'
    )


@pytest.mark.asyncio
async def test_review_create_folder_at_root():
    review = _review({"action": "create", "name": "Work"})
    assert review is not None
    assert review["question"] == (
        'Do you want to create folder "Work" at the library root?'
    )


@pytest.mark.asyncio
async def test_review_move_folder_names_paths():
    work = _folder("Work")
    _folder("Reports", parent_id=work)
    _folder("Archive")
    review = _review(
        {"action": "move", "folder": "Work/Reports", "parent": "Archive"}
    )
    assert review is not None
    assert review["question"] == (
        'Do you want to move folder "Work/Reports" to "Archive/Reports"?'
    )


@pytest.mark.asyncio
async def test_review_rename_folder_names_paths():
    work = _folder("Work")
    _folder("Reports", parent_id=work)
    review = _review(
        {"action": "rename", "folder": "Work/Reports", "name": "Quarterly"}
    )
    assert review is not None
    assert review["question"] == (
        'Do you want to rename folder "Work/Reports" to "Work/Quarterly"?'
    )


@pytest.mark.asyncio
async def test_review_returns_none_for_invalid_or_duplicate():
    _folder("Work")
    assert _review({"action": "create", "name": "Work"}) is None
    assert _review({"action": "move", "folder": "Nope"}) is None
    assert _review({"action": "rename", "folder": "Nope", "name": "X"}) is None
    assert _review({"action": "list"}) is None


# ── agent-loop approval ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_loop_pauses_for_folder_create_approval(monkeypatch):
    import src.agent_loop as agent_loop

    monkeypatch.setattr(
        agent_loop, "get_setting", lambda key, default=None: default, raising=False
    )
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(
        agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False
    )

    async def fake_stream(*args, **kwargs):
        block = '```manage_library_folders\n{"action":"create","name":"Work"}\n```'
        yield "data: " + json.dumps({"delta": block}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def should_not_execute(*args, **kwargs):
        raise AssertionError("folder action executed without approval")

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", should_not_execute)

    events = []
    async for chunk in agent_loop.stream_agent_loop(
        "http://local.test/v1",
        "small-local-model",
        [{"role": "user", "content": "create a Work folder"}],
        max_rounds=1,
        owner="tester",
        relevant_tools={"manage_library_folders", "list_library_files"},
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
    assert payload["question"] == (
        'Do you want to create folder "Work" at the library root?'
    )
    assert [opt["label"] for opt in payload["options"]] == ["Allow once", "Deny"]

    grant = tool_approval_store.consume(
        payload["approval_id"],
        decision="approve_task",
        owner="tester",
        session_id=None,
    )
    assert grant is not None
    assert grant.scope is ToolApprovalScope.SINGLE_ACTION
    assert grant.allow_remaining_actions is False


@pytest.mark.asyncio
async def test_approved_folder_create_executes(monkeypatch):
    import src.agent_loop as agent_loop

    monkeypatch.setattr(
        agent_loop, "get_setting", lambda key, default=None: default, raising=False
    )
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(
        agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False
    )

    responses = [
        '```manage_library_folders\n{"action":"create","name":"Work"}\n```',
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
            [{"role": "user", "content": "create a Work folder"}],
            owner="tester",
            relevant_tools={"manage_library_folders", "list_library_files"},
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
        assert db.query(Folder).count() == 0
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
        folder = db.query(Folder).filter(Folder.name == "Work").first()
        assert folder is not None
        assert folder.parent_id is None
    finally:
        db.close()
