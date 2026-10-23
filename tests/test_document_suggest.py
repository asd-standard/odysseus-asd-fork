"""Suggestion-path coverage for the active editor document.

`suggest_document` shares the read path's target resolution (it must not guess
"the most recent document"), and additionally has to cope with a model that
quotes FIND text copied from a line-numbered `manage_documents read`. Unlike an
edit, a suggestion is non-destructive, so unmatched items are skipped and
reported rather than failing the whole call — the model must not be allowed to
claim it produced suggestions that never landed.
"""

import uuid

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
    SuggestDocumentTool,
    _number_document_lines,
    resolve_find_text,
    set_active_document,
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
    import src.database as sdb

    monkeypatch.setattr(sdb, "SessionLocal", _TS)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    set_active_document(None)
    yield
    set_active_document(None)


def _session(sid="s"):
    db = _TS()
    try:
        if db.query(DbSession).filter(DbSession.id == sid).first() is None:
            db.add(DbSession(id=sid, owner="tester", name=sid, model="m", endpoint_url="http://x"))
            db.commit()
    finally:
        db.close()


def _doc(title, content):
    doc_id = str(uuid.uuid4())
    db = _TS()
    try:
        db.add(
            Document(
                id=doc_id,
                session_id=None,  # detached, like a doc opened from the Library
                title=title,
                language="markdown",
                current_content=content,
                version_count=1,
                is_active=True,
                owner="tester",
            )
        )
        db.commit()
    finally:
        db.close()
    return doc_id


def _blocks(*pairs):
    out = []
    for find, repl in pairs:
        out.append(
            f"<<<FIND>>>\n{find}\n<<<SUGGEST>>>\n{repl}\n<<<REASON>>>\nbecause\n<<<END>>>"
        )
    return "\n".join(out)


def _unique(prefix="doc"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Targeting
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_targets_the_active_editor_document():
    _session()
    doc_id = _doc("Active doc", "hello world")
    set_active_document(doc_id)

    result = await SuggestDocumentTool().execute(
        _blocks(("hello", "hi")), {"owner": "tester"}
    )

    assert result.get("action") == "suggest", result
    assert result["doc_id"] == doc_id
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_suggest_targets_by_title():
    _session()
    name = _unique("suggest-title")
    doc_id = _doc(f"{name}.md", "alpha beta")
    set_active_document(None)  # no editor pointer

    result = await SuggestDocumentTool().execute(
        _blocks(("alpha", "omega")),
        {"owner": "tester", "doc_id": doc_id},
    )

    assert result.get("action") == "suggest", result
    assert result["doc_id"] == doc_id


@pytest.mark.asyncio
async def test_suggest_without_target_uses_the_two_remedies_error():
    _session()
    set_active_document(None)

    result = await SuggestDocumentTool().execute(_blocks(("x", "y")), {"owner": "tester"})

    assert result["exit_code"] == 1
    assert result.get("no_active_document") is True
    assert "title" in result["error"]
    assert "Do NOT fall back" in result["error"]


# --------------------------------------------------------------------------
# Partial matches are reported, not silently dropped
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_match_reports_skipped_items():
    _session()
    set_active_document(_doc("Active doc", "hello world"))

    result = await SuggestDocumentTool().execute(
        _blocks(("hello", "hi"), ("TEXT THAT IS NOT THERE", "nope")),
        {"owner": "tester"},
    )

    # Report-and-continue: the good suggestion still lands...
    assert result.get("action") == "suggest", result
    assert result["count"] == 1
    # ...and the model is told what did not, so it cannot over-claim.
    assert result["skipped"]["count"] == 1
    assert "NOT THERE" in result["skipped"]["snippets"][0]
    assert "skipped" in result["note"].lower()


@pytest.mark.asyncio
async def test_no_match_at_all_is_an_error_not_a_silent_success():
    _session()
    set_active_document(_doc("Active doc", "hello world"))

    result = await SuggestDocumentTool().execute(
        _blocks(("missing entirely", "x")), {"owner": "tester"}
    )

    assert result.get("action") is None
    assert "error" in result
    assert "line number" in result["error"]


# --------------------------------------------------------------------------
# Line-number gutter tolerance (fallback only)
# --------------------------------------------------------------------------


def test_resolve_find_text_prefers_literal_match():
    body = "1\ttext\nplain"
    # Literal wins even when a stripped variant would also match.
    assert resolve_find_text("plain", body) == "plain"
    assert resolve_find_text("1\ttext", body) == "1\ttext"


def test_resolve_find_text_strips_a_copied_gutter():
    body = "alpha\nbeta"
    numbered = _number_document_lines(body)  # "1\talpha\n2\tbeta"
    assert resolve_find_text(numbered, body) == body


def test_gutter_stripping_does_not_fire_when_literal_matches():
    # A document that genuinely begins a line with "42\t" must match literally.
    body = "42\tgenuine"
    assert resolve_find_text("42\tgenuine", body) == "42\tgenuine"


@pytest.mark.asyncio
async def test_suggest_accepts_find_with_copied_line_numbers():
    _session()
    body = "# Title\nSome text here"
    set_active_document(_doc("Active doc", body))

    numbered = _number_document_lines(body)  # what a read returns
    result = await SuggestDocumentTool().execute(
        _blocks((numbered, "# Title\nBetter text here")), {"owner": "tester"}
    )

    assert result.get("action") == "suggest", result
    assert result["count"] == 1
    # The returned suggestion must carry the REAL text, not the numbered form,
    # or the frontend cannot locate it in the editor.
    assert result["suggestions"][0]["find"] == body
