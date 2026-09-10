"""
library_tools.py

Agent tools for the SQLite-backed document library (the same one the Library
modal shows). `list_library_files` browses folders + documents, `read_library_file`
reads a single document or bulk-reads a folder, and `make_library_file` creates a
new document (unfiled). None of these touch the personal-docs filesystem anymore.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.database import SessionLocal, Document, DocumentVersion, Folder

logger = logging.getLogger(__name__)

MAX_READ_CHARS = 20_000
MAX_LIST_FILES = 100

_LANGUAGE_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".html": "html", ".htm": "html", ".css": "css",
    ".json": "json", ".md": "markdown", ".markdown": "markdown", ".txt": "text",
    ".csv": "csv", ".tsv": "tsv", ".log": "log",
    ".sh": "bash", ".bash": "bash", ".nix": "nix",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".ini": "ini",
    ".xml": "xml", ".sql": "sql", ".cpp": "cpp", ".c": "c",
    ".java": "java", ".go": "go", ".rs": "rust", ".php": "php", ".rb": "ruby",
}


def _language_for_name(name: str) -> str:
    _, ext = os.path.splitext((name or "").lower())
    return _LANGUAGE_BY_EXT.get(ext, "text")


def _owned_doc_query(query, owner: Optional[str]):
    if owner is None:
        from sqlalchemy import false
        return query.filter(false())
    return query.filter(Document.owner == owner)


def _owned_folder_query(query, owner: Optional[str]):
    if owner is None:
        from sqlalchemy import false
        return query.filter(false())
    return query.filter(Folder.owner == owner)


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


def _folder_names(db, owner: Optional[str]) -> list[str]:
    """Owner-scoped folder names, for 'not found' hints."""
    q = db.query(Folder.name)
    if owner is not None:
        q = q.filter(Folder.owner == owner)
    q = q.order_by(Folder.name)
    return [name for (name,) in q.all()]


def _resolve_folder(db, owner: Optional[str], folder_arg: str) -> Optional[Folder]:
    """Resolve a folder by id, then exact name, then case-insensitive name."""
    if not folder_arg:
        return None
    f = _owned_folder_query(
        db.query(Folder).filter(Folder.id == folder_arg), owner
    ).first()
    if f is not None:
        return f
    f = _owned_folder_query(
        db.query(Folder).filter(Folder.name == folder_arg), owner
    ).first()
    if f is not None:
        return f
    return _owned_folder_query(
        db.query(Folder).filter(Folder.name.ilike(folder_arg)), owner
    ).first()


def _folder_not_found(db, owner, folder_arg) -> dict:
    available = _folder_names(db, owner)
    hint = ""
    if available:
        hint = " Available folders: " + ", ".join(f"`{n}`" for n in available) + "."
    return {
        "error": f"Folder '{folder_arg}' not found.{hint}",
        "exit_code": 1,
    }


def _page_text(text: str, offset: int, limit: int) -> dict:
    """Slice `text` for paged reads; returns metadata incl. next_offset."""
    offset = max(0, offset)
    end = min(offset + limit, len(text))
    truncated = end < len(text)
    preview = text[offset:end]
    if truncated:
        preview += f"\n... (truncated, {len(text)} chars total; next_offset={end})"
    return {
        "text": preview,
        "size": len(text),
        "truncated": truncated,
        "offset": offset,
        "next_offset": end if truncated else None,
    }


def _parse_args(content: str) -> dict:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {}


def _parse_paging(args: dict):
    try:
        limit = max(1, min(int(args.get("limit", MAX_READ_CHARS)), MAX_READ_CHARS))
    except (TypeError, ValueError):
        limit = MAX_READ_CHARS
    try:
        offset = max(0, int(args.get("offset", 0) or 0))
    except (TypeError, ValueError):
        offset = 0
    return offset, limit


class ListLibraryFilesTool:
    """Tool: list_library_files — list folders and documents in the library.

    With no `folder` argument, lists the root view: top-level folders and unfiled
    documents. With `folder=<id|name>`, lists that folder's child folders and
    documents.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        filter_text = (args.get("filter") or "").strip().lower()
        folder_arg = (args.get("folder") or "").strip()
        owner = ctx.get("owner")

        if folder_arg:
            return self._list_folder(owner, folder_arg, filter_text)
        return self._list_root(owner, filter_text)

    def _list_root(self, owner, filter_text) -> dict:
        db = SessionLocal()
        try:
            folders_q = db.query(Folder)
            if owner is not None:
                folders_q = folders_q.filter(Folder.owner == owner)
            folders = folders_q.filter(Folder.parent_id.is_(None)).order_by(Folder.name).all()

            docs_q = db.query(Document).filter(
                Document.is_active == True, Document.folder_id.is_(None)
            )
            docs_q = _owned_doc_query(docs_q, owner)
            if filter_text:
                docs_q = docs_q.filter(Document.title.ilike(f"%{filter_text}%"))
            docs = docs_q.order_by(Document.updated_at.desc()).limit(MAX_LIST_FILES).all()

            lines = []
            if folders:
                lines.append(f"**Folders ({len(folders)}):**")
                for f in folders:
                    lines.append(f"  - 📁 `{f.name}`")
            if docs:
                lines.append(f"**Unfiled documents ({len(docs)}):**")
                for d in docs:
                    lines.append(
                        f"  - [{d.title}](#document-{d.id}) — {len(d.current_content or '')} chars"
                    )
            if not lines:
                return {"output": "The library is empty.", "exit_code": 0}
            return {"output": "\n".join(lines), "exit_code": 0}
        finally:
            db.close()

    def _list_folder(self, owner, folder_arg, filter_text) -> dict:
        db = SessionLocal()
        try:
            folder = _resolve_folder(db, owner, folder_arg)
            if folder is None:
                return _folder_not_found(db, owner, folder_arg)

            children_q = db.query(Folder).filter(Folder.parent_id == folder.id)
            if owner is not None:
                children_q = children_q.filter(Folder.owner == owner)
            children = children_q.order_by(Folder.name).all()

            docs_q = db.query(Document).filter(
                Document.is_active == True, Document.folder_id == folder.id
            )
            docs_q = _owned_doc_query(docs_q, owner)
            if filter_text:
                docs_q = docs_q.filter(Document.title.ilike(f"%{filter_text}%"))
            docs = docs_q.order_by(Document.updated_at.desc()).limit(MAX_LIST_FILES).all()

            lines = [f"**Folder `{folder.name}` ({len(docs)} file(s)):**"]
            for c in children:
                lines.append(f"  - 📁 `{c.name}`")
            for d in docs:
                lines.append(
                    f"  - [{d.title}](#document-{d.id}) — {d.language or 'text'}, "
                    f"{len(d.current_content or '')} chars"
                )
            if not children and not docs:
                lines.append("  (empty)")
            return {"output": "\n".join(lines), "exit_code": 0}
        finally:
            db.close()


class ReadLibraryFileTool:
    """Tool: read_library_file — read one document, or bulk-read a whole folder.

    Pass `name` for a single document, or `folder` to read every document in a
    folder in one call (paged like a single file).
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        raw = (content or "").strip()
        if raw.startswith("{"):
            args = _parse_args(content)
        else:
            args = {"name": raw}

        name = (args.get("name") or "").strip()
        folder_arg = (args.get("folder") or "").strip()
        owner = ctx.get("owner")

        if folder_arg:
            return await self._read_folder(folder_arg, args, owner)

        if not name:
            return {
                "error": (
                    "read_library_file: provide 'name' (a file) or 'folder' "
                    "(read every file in a folder)."
                ),
                "exit_code": 1,
            }
        return await self._read_single(name, args, owner)

    async def _read_single(self, name, args, owner) -> dict:
        offset, limit = _parse_paging(args)

        db = SessionLocal()
        try:
            doc = None
            q = db.query(Document).filter(Document.is_active == True, Document.title == name)
            q = _owned_doc_query(q, owner)
            doc = q.first()
            if not doc:
                q = db.query(Document).filter(
                    Document.is_active == True, Document.title.ilike(f"%{name}%")
                )
                q = _owned_doc_query(q, owner)
                doc = q.first()
            if not doc:
                q = db.query(Document).filter(
                    Document.is_active == True, Document.id.startswith(name)
                )
                q = _owned_doc_query(q, owner)
                doc = q.first()

            if not doc:
                return {
                    "error": (
                        f"read_library_file: '{name}' not found in the document library. "
                        "Use list_library_files to see available files."
                    ),
                    "exit_code": 1,
                }

            body = doc.current_content or ""
            page = _page_text(body, offset, limit)
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

    async def _read_folder(self, folder_arg, args, owner) -> dict:
        offset, limit = _parse_paging(args)
        db = SessionLocal()
        try:
            folder = _resolve_folder(db, owner, folder_arg)
            if folder is None:
                return _folder_not_found(db, owner, folder_arg)

            docs_q = db.query(Document).filter(
                Document.is_active == True, Document.folder_id == folder.id
            )
            docs_q = _owned_doc_query(docs_q, owner)
            docs = docs_q.order_by(Document.title.asc()).all()

            if not docs:
                return {
                    "output": f"Folder `{folder.name}` is empty.",
                    "exit_code": 0,
                    "document": {"folder": folder.name, "file_count": 0, "size": 0},
                }

            parts = []
            for d in docs:
                parts.append(f"### {d.title} (id: {d.id})\n\n{d.current_content or ''}")
            body = "\n\n".join(parts)
            page = _page_text(body, offset, limit)

            return {
                "output": (
                    f"[Library Folder] `{folder.name}` ({len(docs)} file(s))\n\n{page['text']}"
                ),
                "exit_code": 0,
                "document": {
                    "folder": folder.name,
                    "folder_id": folder.id,
                    "file_count": len(docs),
                    "size": page["size"],
                    "truncated": page["truncated"],
                    "offset": page["offset"],
                    "next_offset": page["next_offset"],
                },
            }
        finally:
            db.close()


class MakeLibraryFileTool:
    """Tool: make_library_file — create a new document in the library (unfiled)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        name = (args.get("name") or "").strip()
        body = args.get("content", "")
        if not name:
            return {"error": "make_library_file: 'name' is required (e.g. 'notes.md').", "exit_code": 1}
        if not body:
            return {"error": "make_library_file: 'content' is required — the file body text.", "exit_code": 1}

        MAX_SIZE = 500_000
        if len(body) > MAX_SIZE:
            return {"error": f"make_library_file: content too large ({len(body)} chars). Max is {MAX_SIZE:,}.", "exit_code": 1}

        owner = ctx.get("owner")
        language = _language_for_name(name)
        doc_id = str(uuid.uuid4())

        db = SessionLocal()
        try:
            doc = Document(
                id=doc_id,
                session_id=None,
                title=name,
                language=language,
                current_content=body,
                version_count=1,
                is_active=True,
                folder_id=None,
                owner=owner,
            )
            ver = DocumentVersion(
                id=str(uuid.uuid4()),
                document_id=doc_id,
                version_number=1,
                content=body,
                summary="Initial version",
                source="user",
            )
            db.add(doc)
            db.add(ver)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"make_library_file failed: {e}")
            return {"error": f"make_library_file: could not save '{name}': {e}", "exit_code": 1}
        finally:
            db.close()

        try:
            from src.event_bus import fire_event
            fire_event("document_created", owner)
        except Exception:
            logger.debug("document_created event dispatch failed", exc_info=True)

        return {
            "output": (
                f"File saved to the library as document `{name}` (id: {doc_id}). "
                "It appears under 'Unfiled documents'. Use list_library_files to verify."
            ),
            "exit_code": 0,
            "document": {"id": doc_id, "title": name, "language": language},
        }
