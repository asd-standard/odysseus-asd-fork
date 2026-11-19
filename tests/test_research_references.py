"""Saved-research references pasted into chat.

`[Query](#research-<id>)` is the same anchor the research panel/agent already
emit. These tests pin the helpers that measure, compact, and inject those saved
reports, plus the owner scoping that keeps one user's report out of another's
prompt.
"""

import json
from pathlib import Path

import pytest

import src.research_utils as ru
from src.research_utils import (
    compact_research_report,
    estimate_research_tokens,
    extract_research_reference_ids,
    format_research_reference,
    load_owned_research,
    research_id_from_reference,
)


def test_reference_ids_ignore_fenced_code_and_dedupe():
    msg = (
        "Compare [A](#research-rp-aaa) with #research-rp-bbb.\n"
        "```\nexample #research-rp-ignored\n```\n"
        "Again #research-rp-aaa."
    )
    assert extract_research_reference_ids(msg) == ["rp-aaa", "rp-bbb"]


def test_research_id_from_reference_accepts_links_and_plain_ids():
    assert research_id_from_reference("#research-rp-abc") == "rp-abc"
    assert research_id_from_reference("[Query](#research-rp-abc)") == "rp-abc"
    assert research_id_from_reference("rp-abc") == "rp-abc"
    assert research_id_from_reference("../etc/passwd") == ""


def test_compact_keeps_structure_and_shrinks_the_report():
    body = "# Findings\n\n" + ("This is a long source summary sentence. " * 2000)
    out = compact_research_report(body, max_chars=800)
    assert "# Findings" in out
    assert len(out) <= 900
    assert len(out) < len(body)
    assert "long source summary" in out


def test_estimate_matches_the_app_char_ratio():
    assert estimate_research_tokens("x" * 1000) == 300


def test_load_owned_research_scopes_owner_and_allows_archived(tmp_path, monkeypatch):
    monkeypatch.setattr(ru, "DEEP_RESEARCH_DIR", str(tmp_path))
    payload = {"query": "q", "result": "body", "owner": "alice", "sources": []}
    (tmp_path / "rp-a.json").write_text(json.dumps(payload), encoding="utf-8")
    archived = dict(payload, archived=True)
    (tmp_path / "rp-b.json").write_text(json.dumps(archived), encoding="utf-8")

    loaded = load_owned_research("rp-a", "alice")
    assert loaded and loaded["id"] == "rp-a"
    assert load_owned_research("rp-a", "bob") is None
    assert load_owned_research("rp-b", "alice")["id"] == "rp-b"
    assert load_owned_research("../rp-a", "alice") is None


def test_format_reference_includes_query_id_report_and_sources():
    text = format_research_reference({
        "id": "rp-a",
        "query": "A query",
        "result": "The report body",
        "sources": [{"title": "Source", "url": "https://example.com"}],
    })
    assert "# A query" in text
    assert "Research id: rp-a" in text
    assert "The report body" in text
    assert "- Source: https://example.com" in text


def test_chat_context_injects_full_or_compacted_reports(monkeypatch):
    from routes import chat_routes as cr

    data = {"id": "rp-a", "query": "A query", "result": "The report body", "sources": []}
    monkeypatch.setattr(cr, "load_owned_research", lambda rid, owner: data if rid == "rp-a" else None)

    full = cr._research_reference_context("see #research-rp-a", "alice", "full")
    assert "The report body" in full
    assert "[Compacted" not in full

    monkeypatch.setattr(cr, "RESEARCH_REFERENCE_TOKEN_BUDGET", 1)
    compact = cr._research_reference_context("see #research-rp-a", "alice", "compact")
    assert "[Compacted" in compact

    # A reference to someone else's/missing report injects nothing.
    assert cr._research_reference_context("see #research-rp-other", "alice", "full") == ""


def test_both_chat_paths_inject_reference_context():
    src = Path("routes/chat_routes.py").read_text(encoding="utf-8")
    # definition + streaming + non-streaming call sites
    assert src.count("_research_reference_context(") >= 3
    assert "research_ref_mode" in src


@pytest.mark.asyncio
async def test_manage_research_accepts_a_pasted_anchor(monkeypatch, tmp_path):
    from src.tools import research as research_tool

    monkeypatch.setattr(research_tool, "DEEP_RESEARCH_DIR", str(tmp_path))
    (tmp_path / "rp-anchor.json").write_text(json.dumps({
        "query": "anchor query",
        "result": "anchor report body",
        "sources": [],
    }), encoding="utf-8")

    res = await research_tool.do_manage_research(json.dumps({
        "action": "read",
        "id": "[Topic](#research-rp-anchor)",
    }))
    assert res.get("exit_code") == 0
    assert "anchor report body" in res.get("output", "")
