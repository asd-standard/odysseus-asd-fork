"""
library_tools.py

Agent tools for browsing and reading files from the personal docs library,
with automatic fallback to the editor document library (SQLite) when the
personal docs index is empty.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from core.database import SessionLocal, Document

logger = logging.getLogger(__name__)

MAX_READ_CHARS = 20_000


def _resolve_manager():
    """Lazy-resolve the PersonalDocsManager from the app-wide global."""
    try:
        from src.ai_interaction import _personal_docs_manager
        return _personal_docs_manager
    except ImportError:
        return None


def _owned_doc_query(query, owner: Optional[str]):
    if owner is None:
        from sqlalchemy import false
        return query.filter(false())
    return query.filter(Document.owner == owner)


def _rel_ts(ts) -> str:
    if not ts:
        return "never"
    try:
        reference = datetime.now(timezone.utc) if ts.tzinfo is not None else datetime.utcnow()
        diff = (reference - ts).total_seconds()
    except Exception:
        return "unknown"
    if diff < 60:    return "just now"
    if diff < 3600:  return f"{int(diff / 60)}m ago"
    if diff < 86400: return f"{int(diff / 3600)}h ago"
    if diff < 86400 * 7: return f"{int(diff / 86400)}d ago"
    return ts.strftime("%Y-%m-%d")


class ListLibraryFilesTool:
    """Tool: list_library_files — list files in the personal docs library,
    with fallback to the editor document library."""

    async def execute(self, content: str, ctx: dict) -> dict:
        mgr = _resolve_manager()
        args = {}
        raw = (content or "").strip()
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        filter_text = (args.get("filter") or "").strip().lower()

        # ── 1. Try personal docs first ──
        if mgr:
            files = mgr.get_file_list()
            if filter_text:
                files = [f for f in files if filter_text in f["name"].lower()]
            if files:
                lines = [f"**Personal Docs Library ({len(files)} files):**"]
                for f in files:
                    size_kb = f["size"] / 1024
                    lines.append(f"  - `{f['name']}` ({size_kb:.1f} KB)")
                return {"output": "\n".join(lines), "exit_code": 0}

        # ── 2. Fallback to editor document library (SQLite) ──
        owner = ctx.get("owner")
        db = SessionLocal()
        try:
            q = db.query(Document).filter(Document.is_active == True)
            q = _owned_doc_query(q, owner)
            if filter_text:
                q = q.filter(Document.title.ilike(f"%{filter_text}%"))
            docs = q.order_by(Document.updated_at.desc()).limit(50).all()

            if not docs:
                source_hint = "Neither personal docs nor the document library contain"
                if filter_text:
                    source_hint += f" files matching '{filter_text}'"
                else:
                    source_hint += " any files"
                return {"output": source_hint + ".", "exit_code": 0}

            lines = [f"**Document Library ({len(docs)} document(s)) — personal docs index was empty:**"]
            for i, d in enumerate(docs):
                size = len(d.current_content or "")
                lang = d.language or "text"
                ts = getattr(d, "updated_at", None) or getattr(d, "created_at", None)
                marker = "  ← most recent" if i == 0 else ""
                lines.append(
                    f"- [{d.title}](#document-{d.id})  — {lang}, {size} chars, updated {_rel_ts(ts)}{marker}"
                )
            return {"output": "\n".join(lines), "exit_code": 0}
        finally:
            db.close()


class ReadLibraryFileTool:
    """Tool: read_library_file — read the content of a specific file (paged),
    with fallback to the editor document library."""

    async def execute(self, content: str, ctx: dict) -> dict:
        mgr = _resolve_manager()
        args = {}
        raw = (content or "").strip()
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        else:
            args = {"name": raw}

        name = (args.get("name") or "").strip()
        if not name:
            return {"error": "read_library_file: 'name' is required — the file name as shown by list_library_files.", "exit_code": 1}

        try:
            preview_limit = max(1, min(int(args.get("limit", MAX_READ_CHARS)), MAX_READ_CHARS))
        except (TypeError, ValueError):
            preview_limit = MAX_READ_CHARS
        try:
            offset = max(0, int(args.get("offset", 0) or 0))
        except (TypeError, ValueError):
            offset = 0

        def _page(body: str) -> dict:
            offset_c = min(offset, len(body))
            end = min(offset_c + preview_limit, len(body))
            truncated = end < len(body)
            preview = body[offset_c:end]
            if truncated:
                preview += f"\n... (truncated, {len(body)} chars total; next_offset={end})"
            return {
                "text": preview,
                "size": len(body),
                "truncated": truncated,
                "offset": offset_c,
                "next_offset": end if truncated else None,
            }

        # ── 1. Try personal docs by exact name ──
        if mgr:
            text = mgr.get_file_content(name)
            if text is not None:
                page = _page(text)
                return {
                    "output": f"[Personal Docs] {name}\n\n{page['text']}",
                    "exit_code": 0,
                    "document": {
                        "name": name,
                        "size": page["size"],
                        "truncated": page["truncated"],
                        "offset": page["offset"],
                        "next_offset": page["next_offset"],
                    },
                }

        # ── 2. Fallback to editor document library ──
        owner = ctx.get("owner")
        db = SessionLocal()
        try:
            # Try exact title match first
            q = db.query(Document).filter(Document.is_active == True, Document.title == name)
            q = _owned_doc_query(q, owner)
            doc = q.first()

            # Loose match: title contains the name
            if not doc:
                q = db.query(Document).filter(
                    Document.is_active == True,
                    Document.title.ilike(f"%{name}%"),
                )
                q = _owned_doc_query(q, owner)
                doc = q.first()

            # Try by id prefix
            if not doc:
                q = db.query(Document).filter(
                    Document.is_active == True,
                    Document.id.startswith(name),
                )
                q = _owned_doc_query(q, owner)
                doc = q.first()

            if not doc:
                return {
                    "error": (
                        f"read_library_file: '{name}' not found in personal docs or document library. "
                        "Use list_library_files to see available files."
                    ),
                    "exit_code": 1,
                }

            body = doc.current_content or ""
            page = _page(body)

            source = f"[Document Library] {doc.title}" if doc.title else "[Document Library]"
            return {
                "output": f"{source} (id: {doc.id})\n\n```{doc.language or ''}\n{page['text']}\n```",
                "exit_code": 0,
                "document": {
                    "id": doc.id,
                    "title": doc.title,
                    "language": doc.language,
                    "size": page["size"],
                    "truncated": page["truncated"],
                    "offset": page["offset"],
                    "next_offset": page["next_offset"],
                },
            }
        finally:
            db.close()


class MakeLibraryFileTool:
    """Tool: make_library_file — save a text file into the personal docs library."""

    async def execute(self, content: str, ctx: dict) -> dict:
        mgr = _resolve_manager()
        if not mgr:
            return {"error": "Personal docs library not available. RAG may not be configured.", "exit_code": 1}

        args = {}
        raw = (content or "").strip()
        if raw.startswith("{"):
            try:
                args = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        name = (args.get("name") or "").strip()
        body = args.get("content", "")
        if not name:
            return {"error": "make_library_file: 'name' is required (e.g. 'notes.md').", "exit_code": 1}
        if not body:
            return {"error": "make_library_file: 'content' is required — the file body text.", "exit_code": 1}

        MAX_SIZE = 500_000
        if len(body) > MAX_SIZE:
            return {"error": f"make_library_file: content too large ({len(body)} chars). Max is {MAX_SIZE:,}.", "exit_code": 1}

        result = mgr.add_file(name, body)
        if result.get("error"):
            return {"error": result["error"], "exit_code": 1}

        return {
            "output": f"File saved to library: `{name}` ({result['size']} chars). Use list_library_files to verify.",
            "exit_code": 0,
        }
