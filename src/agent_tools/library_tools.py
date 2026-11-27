"""
library_tools.py

Agent tools for the SQLite-backed document library (the same one the Library
modal shows). `list_library_files` browses folders + documents, `read_library_file`
reads a single document or bulk-reads a folder, and `make_library_file` creates a
new document (Unfiled by default, or directly in a named folder). None of these
touch the personal-docs filesystem anymore.
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

# Values accepted as "the library root" for folder arguments. Matching is
# case-insensitive and after trimming whitespace/slashes.
UNFILED_FOLDER_ALIASES = frozenset(
    {"", "unfiled", "unfiled (root)", "all files", "root", "library root", "/"}
)

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


def _normalized_folder_arg(value) -> str:
    return str(value or "").strip()


def is_unfiled_folder(value) -> bool:
    """True when *value* means the library root rather than a real folder."""
    return _normalized_folder_arg(value).casefold() in UNFILED_FOLDER_ALIASES


def _folder_rows(db, owner: Optional[str]):
    """Return ``(by_id, path_by_id)`` for every folder owned by *owner*.

    Paths are "/"-joined names from the library root, e.g. ``Work/Reports``.
    Folders are logical (SQLite) locations, not filesystem directories.
    """
    folders = _owned_folder_query(db.query(Folder), owner).all()
    by_id = {f.id: f for f in folders}

    def path_for(folder: Folder) -> str:
        parts = []
        seen = set()
        cur = folder
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            parts.append(cur.name)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        return "/".join(reversed(parts))

    return by_id, {f.id: path_for(f) for f in folders}


def folder_path(db, owner: Optional[str], folder: Optional[Folder]) -> str:
    """Human/LLM-facing path for *folder* ("" when it cannot be resolved)."""
    if folder is None:
        return ""
    _, paths = _folder_rows(db, owner)
    return paths.get(folder.id) or folder.name or ""


def _folder_choices(db, owner: Optional[str], limit: int = 30) -> list[str]:
    """Ready-to-print folder paths with ids, for error hints."""
    _, paths = _folder_rows(db, owner)
    rows = sorted(paths.items(), key=lambda item: (item[1].casefold(), item[0]))
    out = [f"`{path}` (id: {fid})" for fid, path in rows[:limit]]
    if len(rows) > limit:
        out.append(f"...and {len(rows) - limit} more")
    return out


def _ambiguous_folder_error(db, owner, folder_arg: str, matches: list[Folder]) -> dict:
    listing = ", ".join(
        f"`{folder_path(db, owner, f)}` (id: {f.id})" for f in matches[:20]
    )
    return {
        "error": (
            f"More than one folder is named '{folder_arg}'. "
            f"Pass a folder id or a full path instead: {listing}."
        ),
        "exit_code": 1,
        "ambiguous_folder": True,
    }


def _resolve_folder(db, owner: Optional[str], folder_arg: str) -> Optional[Folder]:
    """Resolve a folder by id, full path, then exact/case-insensitive name.

    This is the lenient resolver used by read/list tools. Write paths use
    :func:`_resolve_folder_strict` so an ambiguous bare name can never silently
    target the wrong folder.
    """
    arg = _normalized_folder_arg(folder_arg)
    if not arg:
        return None
    f = _owned_folder_query(
        db.query(Folder).filter(Folder.id == arg), owner
    ).first()
    if f is not None:
        return f

    by_id, paths = _folder_rows(db, owner)
    target = arg.strip().strip("/").casefold()
    if "/" in target:
        for fid, path in paths.items():
            if path.casefold() == target:
                return by_id[fid]

    f = _owned_folder_query(
        db.query(Folder).filter(Folder.name == arg), owner
    ).first()
    if f is not None:
        return f
    return _owned_folder_query(
        db.query(Folder).filter(Folder.name.ilike(arg)), owner
    ).first()


def _resolve_folder_strict(
    db,
    owner: Optional[str],
    folder_arg: str,
) -> tuple[Optional[Folder], Optional[dict]]:
    """Resolve a write destination, rejecting ambiguous bare names.

    Returns ``(folder, None)`` on success and ``(None, error)`` when the folder
    does not exist or its name is ambiguous.
    """
    arg = _normalized_folder_arg(folder_arg)
    if not arg:
        return None, None
    f = _owned_folder_query(
        db.query(Folder).filter(Folder.id == arg), owner
    ).first()
    if f is not None:
        return f, None

    by_id, paths = _folder_rows(db, owner)
    target = arg.strip().strip("/").casefold()
    if "/" in target:
        for fid, path in paths.items():
            if path.casefold() == target:
                return by_id[fid], None
        return None, _folder_not_found(db, owner, arg)

    choices = _owned_folder_query(db.query(Folder), owner).all()
    exact = [f for f in choices if f.name == arg]
    matches = exact or [f for f in choices if f.name.casefold() == target]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, _ambiguous_folder_error(db, owner, arg, matches)
    return None, _folder_not_found(db, owner, arg)


def _folder_not_found(db, owner, folder_arg) -> dict:
    available = _folder_choices(db, owner)
    hint = ""
    if available:
        hint = " Available folders: " + ", ".join(available) + "."
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

            _, folder_paths = _folder_rows(db, owner)

            lines = []
            if folders:
                lines.append(f"**Folders ({len(folders)}):**")
                for f in folders:
                    path = folder_paths.get(f.id) or f.name
                    lines.append(f"  - 📁 `{path}` (id: {f.id})")
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

            _, folder_paths = _folder_rows(db, owner)
            folder_label = folder_paths.get(folder.id) or folder.name
            lines = [f"**Folder `{folder_label}` ({len(docs)} file(s)):**"]
            for c in children:
                child_path = folder_paths.get(c.id) or c.name
                lines.append(f"  - 📁 `{child_path}` (id: {c.id})")
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

            _, folder_paths = _folder_rows(db, owner)
            folder_label = folder_paths.get(folder.id) or folder.name

            if not docs:
                return {
                    "output": f"Folder `{folder_label}` is empty.",
                    "exit_code": 0,
                    "document": {"folder": folder_label, "file_count": 0, "size": 0},
                }

            parts = []
            for d in docs:
                parts.append(f"### {d.title} (id: {d.id})\n\n{d.current_content or ''}")
            body = "\n\n".join(parts)
            page = _page_text(body, offset, limit)

            return {
                "output": (
                    f"[Library Folder] `{folder_label}` ({len(docs)} file(s))\n\n{page['text']}"
                ),
                "exit_code": 0,
                "document": {
                    "folder": folder_label,
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
    """Tool: make_library_file — create a new document in the library.

    With no ``folder`` (or ``folder`` = unfiled/root) the document lands in
    Unfiled. A folder id, full path (``Work/Reports``), or unique name files it
    directly there.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        name = (args.get("name") or "").strip()
        body = args.get("content", "")
        folder_arg = args.get("folder") or args.get("folder_id")
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
        folder = None
        folder_id_value = None
        folder_label = "Unfiled"

        db = SessionLocal()
        try:
            if not is_unfiled_folder(folder_arg):
                folder, folder_error = _resolve_folder_strict(db, owner, folder_arg)
                if folder_error:
                    return folder_error
                folder_id_value = folder.id
                folder_label = folder_path(db, owner, folder) or folder.name

            doc = Document(
                id=doc_id,
                session_id=None,
                title=name,
                language=language,
                current_content=body,
                version_count=1,
                is_active=True,
                folder_id=folder_id_value,
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

        destination = (
            f"the folder `{folder_label}`"
            if folder is not None
            else "'Unfiled documents'"
        )
        return {
            "output": (
                f"File saved to the library as document `{name}` in {destination} "
                f"(id: {doc_id}). Use list_library_files to verify."
            ),
            "exit_code": 0,
            "document": {
                "id": doc_id,
                "title": name,
                "language": language,
                "folder_id": folder_id_value,
                "folder": folder_label if folder is not None else None,
            },
        }


def library_action_approval_review(
    tool_name: str,
    content: str,
    owner: Optional[str],
) -> Optional[dict]:
    """Describe the human approval a library folder action needs.

    Returns ``None`` when the action does not change a file's folder (for
    example creating in Unfiled) or when its target cannot be resolved — in the
    latter case the tool itself returns the actionable error. A returned dict
    is consumed by the agent loop to build a server-sealed approval card.
    """
    from src.tool_approvals import LIBRARY_FOLDER_APPROVAL_CATEGORY

    if tool_name == "make_library_file":
        args = _parse_args(content)
        folder_arg = args.get("folder") or args.get("folder_id")
        if is_unfiled_folder(folder_arg):
            return None
        name = str(args.get("name") or "").strip() or "file"
        body = str(args.get("content") or "")
        db = SessionLocal()
        try:
            folder, error = _resolve_folder_strict(db, owner, folder_arg)
            if error or folder is None:
                return None
            destination = folder_path(db, owner, folder) or folder.name
        finally:
            db.close()
        return {
            "category": LIBRARY_FOLDER_APPROVAL_CATEGORY,
            "question": f'Do you want to create "{name}" in "{destination}"?',
            "description": (
                "The assistant wants to create a file in a library folder. "
                "Approving runs only this exact action once."
            ),
            "display_content": (
                f'create "{name}" ({len(body):,} chars) in "{destination}"'
            ),
        }

    if tool_name == "manage_documents":
        args = _parse_args(content)
        action = str(args.get("action") or "").strip().casefold()
        if action != "move":
            return None
        from src.agent_tools.document_tools import _resolve_move_request

        db = SessionLocal()
        try:
            doc, folder, destination, error = _resolve_move_request(db, owner, args)
            if error or doc is None:
                return None
            doc_title = doc.title
        finally:
            db.close()
        return {
            "category": LIBRARY_FOLDER_APPROVAL_CATEGORY,
            "question": f'Do you want to move "{doc_title}" to "{destination}"?',
            "description": (
                "The assistant wants to move a document to another library "
                "folder. Approving runs only this exact action once."
            ),
            "display_content": f'move "{doc_title}" to "{destination}"',
        }

    return None
