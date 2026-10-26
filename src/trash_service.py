"""Shared Trash policy for Library documents and research.

Deleting from the Library is reversible for a retention window. Documents are
soft-deleted in SQLite (``is_active=False`` + ``deleted_at``); research reports
are JSON files moved into ``data/deep_research/.trash/`` with a
``trash_deleted_at`` stamp. Purging past the window hard-deletes them.

This module is the single source of truth for the retention setting and both
purge routines so routes, agent tools, and the startup loop agree.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

SETTINGS_KEY = "trash_retention_days"
DEFAULT_RETENTION_DAYS = 15
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650

_RESEARCH_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def _utcnow() -> datetime:
    """Naive UTC, matching the DateTime columns used by documents."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def retention_days() -> int:
    """Retention window in days, clamped to a sane range."""
    try:
        from src.settings import get_setting

        days = int(get_setting(SETTINGS_KEY, DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    except Exception:
        return DEFAULT_RETENTION_DAYS
    return max(MIN_RETENTION_DAYS, min(days, MAX_RETENTION_DAYS))


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def move_document_to_trash(doc, now: Optional[datetime] = None) -> bool:
    """Mark a document deleted. Returns False when it was already trashed."""
    already = not getattr(doc, "is_active", True) and getattr(doc, "deleted_at", None)
    if already:
        return False
    doc.is_active = False
    doc.deleted_at = now or _utcnow()
    return True


def restore_document(doc) -> None:
    """Undo a trash move. Archive state and folder are left untouched."""
    doc.is_active = True
    doc.deleted_at = None


def purge_expired_documents(db, owner: Optional[str] = None,
                            now: Optional[datetime] = None) -> int:
    """Hard-delete documents whose trash window has elapsed.

    Iterates ORM rows so the ``versions`` relationship cascade runs; bulk
    DELETE would leave version rows behind on SQLite unless FK enforcement is
    enabled.
    """
    from core.database import Document

    cutoff = (now or _utcnow()) - timedelta(days=retention_days())
    q = db.query(Document).filter(
        Document.is_active == False,  # noqa: E712 - SQLAlchemy column compare
        Document.deleted_at.isnot(None),
        Document.deleted_at < cutoff,
    )
    if owner is not None:
        q = q.filter(Document.owner == owner)
    rows = q.all()
    for doc in rows:
        db.delete(doc)
    if rows:
        db.commit()
    return len(rows)


def trash_metadata(deleted_at: Optional[datetime],
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Serialize trash timestamps for the frontend."""
    if deleted_at is None:
        return {"deleted_at": None, "expires_at": None, "days_left": None}
    now = now or _utcnow()
    expires = deleted_at + timedelta(days=retention_days())
    seconds_left = (expires - now).total_seconds()
    days_left = max(0, math.ceil(seconds_left / 86400)) if seconds_left > 0 else 0
    return {
        "deleted_at": deleted_at.isoformat() + "Z",
        "expires_at": expires.isoformat() + "Z",
        "days_left": days_left,
    }


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

def research_root() -> Path:
    from src.constants import DEEP_RESEARCH_DIR

    return Path(DEEP_RESEARCH_DIR).resolve()


def research_trash_dir() -> Path:
    return research_root() / ".trash"


def _valid_research_id(session_id: str) -> bool:
    return bool(_RESEARCH_ID_RE.fullmatch(str(session_id or "")))


def find_active_research_file(session_id: str) -> Optional[Path]:
    """Return the active JSON file for ``session_id``, confined to the root."""
    if not _valid_research_id(session_id):
        return None
    root = research_root()
    path = root / f"{session_id}.json"
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def find_trashed_research_file(session_id: str) -> Optional[Path]:
    if not _valid_research_id(session_id):
        return None
    trash = research_trash_dir()
    path = trash / f"{session_id}.json"
    try:
        resolved = path.resolve()
        resolved.relative_to(trash.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def move_research_path_to_trash(path: Path, owner: Optional[str]) -> bool:
    """Move an already-validated active research file into ``.trash``.

    The caller is responsible for path confinement (see
    ``routes.research.research_routes._find_research_path``). Raises
    ``PermissionError`` for an owner mismatch and ``FileExistsError`` when a
    stale trash copy occupies the slot.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read research %s for trash: %s", path.name, exc)
        return False
    if data.get("owner") != owner:
        raise PermissionError("Research not found")

    trash = path.parent / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / path.name
    if target.exists():
        raise FileExistsError(f"Research {path.stem} is already in Trash")

    data["trash_deleted_at"] = time.time()
    atomic_write_json(str(target), data, indent=2)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


def move_research_to_trash(session_id: str, owner: Optional[str]) -> bool:
    """Move a research JSON into ``.trash`` and stamp its deletion time.

    Returns False when the report does not exist / is not owned by ``owner``.
    Raises FileExistsError when a stale trash copy already occupies the slot.
    """
    path = find_active_research_file(session_id)
    if path is None:
        return False
    try:
        return move_research_path_to_trash(path, owner)
    except PermissionError:
        return False


def restore_research(session_id: str, owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """Move a report back out of Trash. Returns its data, or None if absent."""
    trash_path = find_trashed_research_file(session_id)
    if trash_path is None:
        return None
    try:
        data = json.loads(trash_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read trashed research %s: %s", session_id, exc)
        return None
    if data.get("owner") != owner:
        return None

    target = research_root() / trash_path.name
    if target.exists():
        raise FileExistsError(f"A research report with id {session_id} already exists")
    data.pop("trash_deleted_at", None)
    atomic_write_json(str(target), data, indent=2)
    try:
        trash_path.unlink()
    except FileNotFoundError:
        pass
    return data


def permanently_delete_research(session_id: str, owner: Optional[str]) -> bool:
    trash_path = find_trashed_research_file(session_id)
    if trash_path is None:
        return False
    try:
        data = json.loads(trash_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if data.get("owner") != owner:
        return False
    trash_path.unlink(missing_ok=True)
    return True


def _research_deleted_at(data: Dict[str, Any], path: Path) -> float:
    raw = data.get("trash_deleted_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            pass
    # Legacy/partially written entries: fall back to the file mtime.
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _research_metadata(deleted_epoch: float,
                       now_epoch: Optional[float] = None) -> Dict[str, Any]:
    now_epoch = time.time() if now_epoch is None else now_epoch
    expires_epoch = deleted_epoch + retention_days() * 86400
    seconds_left = expires_epoch - now_epoch
    days_left = max(0, math.ceil(seconds_left / 86400)) if seconds_left > 0 else 0
    return {
        "deleted_at": datetime.fromtimestamp(deleted_epoch, timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(expires_epoch, timezone.utc).isoformat(),
        "days_left": days_left,
    }


def list_research_trash(owner: Optional[str]) -> list[Dict[str, Any]]:
    """Return the owner's trashed research reports, newest deletion first."""
    trash = research_trash_dir()
    if not trash.is_dir():
        return []
    items: list[Dict[str, Any]] = []
    for path in trash.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("owner") != owner:
            continue
        deleted_epoch = _research_deleted_at(data, path)
        meta = _research_metadata(deleted_epoch)
        items.append({
            "id": path.stem,
            "query": data.get("query") or "Untitled research",
            "category": data.get("category") or "",
            "source_count": len(data.get("sources") or []),
            "archived": bool(data.get("archived")),
            **meta,
        })
    items.sort(key=lambda item: item["deleted_at"], reverse=True)
    return items


def purge_expired_research(owner: Optional[str] = None,
                           now_epoch: Optional[float] = None) -> int:
    trash = research_trash_dir()
    if not trash.is_dir():
        return 0
    now_epoch = time.time() if now_epoch is None else now_epoch
    cutoff = now_epoch - retention_days() * 86400
    removed = 0
    for path in trash.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if owner is not None and data.get("owner") != owner:
            continue
        if _research_deleted_at(data, path) < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("Failed to purge trashed research %s: %s", path.name, exc)
    return removed


def empty_research_trash(owner: Optional[str]) -> int:
    trash = research_trash_dir()
    if not trash.is_dir():
        return 0
    removed = 0
    for path in trash.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if data.get("owner") != owner:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def purge_all_expired() -> Dict[str, int]:
    """Purge expired documents and research globally (startup/daily job)."""
    from core.database import SessionLocal

    documents = 0
    db = SessionLocal()
    try:
        documents = purge_expired_documents(db)
    except Exception as exc:
        logger.warning("Document trash purge failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()
    research = 0
    try:
        research = purge_expired_research()
    except Exception as exc:
        logger.warning("Research trash purge failed: %s", exc)
    if documents or research:
        logger.info("Trash purge: removed %s document(s), %s research report(s)", documents, research)
    return {"documents": documents, "research": research}
