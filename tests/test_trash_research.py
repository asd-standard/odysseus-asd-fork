"""Library Trash — research reports (file-based soft delete)."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import src.constants as constants
import routes.research.research_routes as rr
from src import trash_service as trash


@pytest.fixture(autouse=True)
def _research_dir(monkeypatch, tmp_path):
    root = tmp_path / "deep_research"
    root.mkdir()
    monkeypatch.setattr(rr, "DEEP_RESEARCH_DIR", str(root))
    monkeypatch.setattr(constants, "DEEP_RESEARCH_DIR", str(root))
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    return root


def _req(user="tester"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _endpoint(method, path):
    router = rr.setup_research_routes(MagicMock(), MagicMock())
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _call(method, path, *args, **kwargs):
    return asyncio.run(_endpoint(method, path)(*args, **kwargs))


def _make_report(root, rid="rp-abc123", owner="tester", query="Test research",
                 archived=False, trash_deleted_at=None):
    data = {
        "owner": owner,
        "query": query,
        "archived": archived,
        "sources": [{"url": "https://example.com", "title": "Example"}],
        "result": "Report body",
    }
    if trash_deleted_at is not None:
        data["trash_deleted_at"] = trash_deleted_at
    path = root / f"{rid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _library(request, archived=False):
    return _call(
        "GET", "/api/research/library", request,
        None, "recent", 50, archived,
    )


def test_delete_moves_report_to_trash_and_hides_it(_research_dir):
    _make_report(_research_dir)

    result = _call("DELETE", "/api/research/{session_id}", "rp-abc123", _req())
    assert result["deleted"] is True
    assert not (_research_dir / "rp-abc123.json").exists()
    assert (_research_dir / ".trash" / "rp-abc123.json").exists()

    library = _library(_req())
    assert library["research"] == []

    listed = _call("GET", "/api/research/trash", _req(), None, "recent", 200)
    assert listed["total"] == 1
    assert listed["research"][0]["id"] == "rp-abc123"
    assert listed["research"][0]["days_left"] == 15
    assert listed["retention_days"] == 15


def test_restore_preserves_archive_state_and_removes_trash_stamp(_research_dir):
    _make_report(_research_dir, archived=True)
    _call("DELETE", "/api/research/{session_id}", "rp-abc123", _req())

    result = _call("POST", "/api/research/{session_id}/restore", "rp-abc123", _req())
    assert result == {"ok": True, "id": "rp-abc123", "archived": True}

    active = _research_dir / "rp-abc123.json"
    assert active.exists()
    data = json.loads(active.read_text(encoding="utf-8"))
    assert data["archived"] is True
    assert "trash_deleted_at" not in data
    assert not (_research_dir / ".trash" / "rp-abc123.json").exists()

    # Archived items are still hidden from the default library view.
    assert _library(_req())["research"] == []
    assert len(_library(_req(), archived=True)["research"]) == 1


def test_permanent_delete_requires_trash(_research_dir):
    _make_report(_research_dir)
    with pytest.raises(HTTPException) as exc:
        _call("DELETE", "/api/research/{session_id}/permanent", "rp-abc123", _req())
    assert exc.value.status_code == 404

    _call("DELETE", "/api/research/{session_id}", "rp-abc123", _req())
    result = _call("DELETE", "/api/research/{session_id}/permanent", "rp-abc123", _req())
    assert result["deleted"] is True
    assert not (_research_dir / ".trash" / "rp-abc123.json").exists()


def test_research_trash_is_owner_scoped(_research_dir):
    _make_report(_research_dir, rid="rp-mine", owner="tester")
    _make_report(_research_dir, rid="rp-theirs", owner="someone-else")
    _call("DELETE", "/api/research/{session_id}", "rp-mine", _req("tester"))
    _call("DELETE", "/api/research/{session_id}", "rp-theirs", _req("someone-else"))

    listed = _call("GET", "/api/research/trash", _req("tester"), None, "recent", 200)
    assert [r["id"] for r in listed["research"]] == ["rp-mine"]

    with pytest.raises(HTTPException) as exc:
        _call("POST", "/api/research/{session_id}/restore", "rp-theirs", _req("tester"))
    assert exc.value.status_code == 404


def test_empty_research_trash_only_deletes_callers_reports(_research_dir):
    _make_report(_research_dir, rid="rp-mine", owner="tester")
    _make_report(_research_dir, rid="rp-theirs", owner="someone-else")
    _call("DELETE", "/api/research/{session_id}", "rp-mine", _req("tester"))
    _call("DELETE", "/api/research/{session_id}", "rp-theirs", _req("someone-else"))

    result = _call("POST", "/api/research/trash/empty", _req("tester"))
    assert result["deleted"] == 1
    assert not (_research_dir / ".trash" / "rp-mine.json").exists()
    assert (_research_dir / ".trash" / "rp-theirs.json").exists()


def test_purge_expired_research_respects_cutoff_and_owner(_research_dir):
    trash_dir = _research_dir / ".trash"
    trash_dir.mkdir()
    old = time.time() - 16 * 86400
    fresh = time.time() - 14 * 86400
    _make_report(trash_dir, rid="rp-old", trash_deleted_at=old)
    _make_report(trash_dir, rid="rp-fresh", trash_deleted_at=fresh)
    _make_report(trash_dir, rid="rp-other", owner="someone-else", trash_deleted_at=old)

    assert trash.purge_expired_research(owner="tester") == 1
    assert not (trash_dir / "rp-old.json").exists()
    assert (trash_dir / "rp-fresh.json").exists()
    assert (trash_dir / "rp-other.json").exists()


def test_trashed_research_is_not_served_by_detail_or_report(_research_dir):
    _make_report(_research_dir)
    _call("DELETE", "/api/research/{session_id}", "rp-abc123", _req())

    with pytest.raises(HTTPException) as detail_exc:
        _call("GET", "/api/research/detail/{session_id}", "rp-abc123", _req())
    assert detail_exc.value.status_code == 404

    with pytest.raises(HTTPException) as report_exc:
        _call("GET", "/api/research/report/{session_id}", "rp-abc123", _req())
    assert report_exc.value.status_code == 404
