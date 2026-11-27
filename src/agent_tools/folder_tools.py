"""Agent tool for managing the SQLite-backed document-library folder tree.

`manage_library_folders` creates, moves, and renames folders. Every action is a
structural write and is surfaced to the user through the same one-use approval
card used for folder-aware file writes.

The resolver functions are shared with the approval review in
``library_tools.py`` so the card describes exactly the folder operation that
will execute.
"""

import uuid
from typing import Optional

from core.database import Folder, SessionLocal
from src.agent_tools.library_tools import (
    _folder_not_found,
    _owned_folder_query,
    _parse_args,
    _resolve_folder_strict,
    folder_path,
    is_unfiled_folder,
)

MAX_FOLDER_NAME_LENGTH = 120


def _clean_folder_name(value) -> tuple[Optional[str], Optional[dict]]:
    name = str(value or "").strip()
    if not name:
        return None, {"error": "Folder name is required", "exit_code": 1}
    if len(name) > MAX_FOLDER_NAME_LENGTH:
        return None, {
            "error": (
                f"Folder name must be {MAX_FOLDER_NAME_LENGTH} characters or fewer"
            ),
            "exit_code": 1,
        }
    return name, None


def _sibling_folder(
    db,
    owner: Optional[str],
    parent_id: Optional[str],
    name: str,
    exclude_id: Optional[str] = None,
) -> Optional[Folder]:
    """Find an owner-scoped sibling using the same name under `parent_id`."""
    q = _owned_folder_query(db.query(Folder).filter(Folder.name == name), owner)
    if parent_id is None:
        q = q.filter(Folder.parent_id.is_(None))
    else:
        q = q.filter(Folder.parent_id == parent_id)
    if exclude_id:
        q = q.filter(Folder.id != exclude_id)
    return q.first()


def _resolve_parent(db, owner: Optional[str], raw):
    """Resolve a create/move destination parent; None means the root."""
    if is_unfiled_folder(raw):
        return None, None
    return _resolve_folder_strict(db, owner, raw)


def _descendant_ids(folders: list[Folder], root_id: str) -> set[str]:
    children: dict[Optional[str], list[str]] = {}
    for folder in folders:
        children.setdefault(folder.parent_id, []).append(folder.id)
    seen = set()
    stack = list(children.get(root_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, []))
    return seen


def _missing_folder_error(folder_arg) -> dict:
    return {
        "error": (
            "manage_library_folders: 'folder' is required — pass a folder "
            "id, full path (e.g. 'Work/Reports'), or unique name. Use "
            "list_library_files to see folder paths and ids."
        ),
        "exit_code": 1,
    }


def resolve_folder_create(db, owner: Optional[str], args: dict):
    """Return ``(plan, error)`` for a create-folder request."""
    name, error = _clean_folder_name(args.get("name"))
    if error:
        return None, error

    parent_arg = args.get("parent")
    if parent_arg is None:
        parent_arg = args.get("parent_id")
    parent, parent_error = _resolve_parent(db, owner, parent_arg)
    if parent_error:
        return None, parent_error

    parent_id = parent.id if parent is not None else None
    conflict = _sibling_folder(db, owner, parent_id, name)
    if conflict is not None:
        return None, {
            "error": f"A folder named '{name}' already exists here",
            "exit_code": 1,
        }

    parent_path = folder_path(db, owner, parent) if parent is not None else ""
    return {
        "name": name,
        "parent": parent,
        "parent_id": parent_id,
        "parent_path": parent_path,
    }, None


def resolve_folder_move(db, owner: Optional[str], args: dict):
    """Return ``(plan, error)`` for a move-folder request."""
    folder_arg = args.get("folder") or args.get("folder_id")
    if not folder_arg:
        return None, _missing_folder_error(folder_arg)
    folder, folder_error = _resolve_folder_strict(db, owner, folder_arg)
    if folder_error:
        return None, folder_error
    if folder is None:
        return None, _folder_not_found(db, owner, folder_arg)

    has_destination = any(
        key in args for key in ("parent", "parent_id", "to", "destination")
    )
    if not has_destination:
        return None, {
            "error": (
                "manage_library_folders move: a destination is required. "
                "Pass 'parent'/'parent_id' with a folder id, path, or unique "
                "name, or 'unfiled'/'root' for the library root."
            ),
            "exit_code": 1,
        }
    destination_arg = (
        args.get("parent")
        if "parent" in args
        else args.get("parent_id")
        if "parent_id" in args
        else args.get("to")
        if "to" in args
        else args.get("destination")
    )
    parent, parent_error = _resolve_parent(db, owner, destination_arg)
    if parent_error:
        return None, parent_error

    if parent is not None:
        if parent.id == folder.id:
            return None, {
                "error": "A folder cannot be moved into itself",
                "exit_code": 1,
            }
        all_folders = _owned_folder_query(db.query(Folder), owner).all()
        if parent.id in _descendant_ids(all_folders, folder.id):
            return None, {
                "error": "A folder cannot be moved into its own subfolder",
                "exit_code": 1,
            }

    new_parent_id = parent.id if parent is not None else None
    conflict = _sibling_folder(
        db, owner, new_parent_id, folder.name, exclude_id=folder.id
    )
    if conflict is not None:
        return None, {
            "error": (
                f"A folder named '{folder.name}' already exists in the "
                "destination"
            ),
            "exit_code": 1,
        }

    old_path = folder_path(db, owner, folder)
    parent_path = folder_path(db, owner, parent) if parent is not None else ""
    new_path = f"{parent_path}/{folder.name}" if parent_path else folder.name
    return {
        "folder": folder,
        "parent": parent,
        "old_path": old_path,
        "new_path": new_path,
    }, None


def resolve_folder_rename(db, owner: Optional[str], args: dict):
    """Return ``(plan, error)`` for a rename-folder request."""
    folder_arg = (
        args.get("folder") or args.get("folder_id") or args.get("id")
    )
    if not folder_arg:
        return None, _missing_folder_error(folder_arg)
    folder, folder_error = _resolve_folder_strict(db, owner, folder_arg)
    if folder_error:
        return None, folder_error
    if folder is None:
        return None, _folder_not_found(db, owner, folder_arg)

    new_name, name_error = _clean_folder_name(
        args.get("name") or args.get("new_name")
    )
    if name_error:
        return None, name_error

    conflict = _sibling_folder(
        db, owner, folder.parent_id, new_name, exclude_id=folder.id
    )
    if conflict is not None:
        return None, {
            "error": f"A folder named '{new_name}' already exists here",
            "exit_code": 1,
        }

    old_path = folder_path(db, owner, folder)
    parent_path = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
    new_path = f"{parent_path}/{new_name}" if parent_path else new_name
    return {
        "folder": folder,
        "new_name": new_name,
        "old_path": old_path,
        "new_path": new_path,
    }, None


class ManageLibraryFoldersTool:
    """Tool: manage_library_folders — create, move, or rename library folders."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _parse_args(content)
        action = str(args.get("action") or "").strip().casefold()
        owner = ctx.get("owner")

        db = SessionLocal()
        try:
            if action == "create":
                plan, error = resolve_folder_create(db, owner, args)
                if error:
                    return error
                folder_id = str(uuid.uuid4())
                folder = Folder(
                    id=folder_id,
                    name=plan["name"],
                    parent_id=plan["parent_id"],
                    owner=owner,
                    sort_order=0,
                )
                db.add(folder)
                db.commit()
                path = folder_path(db, owner, folder)
                return {
                    "output": (
                        f"Created folder `{path}` (id: {folder_id}). "
                        "Use list_library_files to verify."
                    ),
                    "exit_code": 0,
                    "folder": {
                        "id": folder_id,
                        "name": plan["name"],
                        "path": path,
                        "parent_id": plan["parent_id"],
                    },
                }

            if action == "move":
                plan, error = resolve_folder_move(db, owner, args)
                if error:
                    return error
                folder = plan["folder"]
                folder_id = folder.id
                parent = plan["parent"]
                folder.parent_id = parent.id if parent is not None else None
                db.commit()
                path = folder_path(db, owner, folder)
                return {
                    "output": (
                        f"Moved folder `{plan['old_path']}` to `{path}` "
                        f"(id: {folder_id})."
                    ),
                    "exit_code": 0,
                    "folder": {
                        "id": folder_id,
                        "name": folder.name,
                        "path": path,
                        "parent_id": folder.parent_id,
                    },
                }

            if action == "rename":
                plan, error = resolve_folder_rename(db, owner, args)
                if error:
                    return error
                folder = plan["folder"]
                folder_id = folder.id
                folder.name = plan["new_name"]
                db.commit()
                path = folder_path(db, owner, folder)
                return {
                    "output": (
                        f"Renamed folder `{plan['old_path']}` to `{path}` "
                        f"(id: {folder_id})."
                    ),
                    "exit_code": 0,
                    "folder": {
                        "id": folder_id,
                        "name": plan["new_name"],
                        "path": path,
                        "parent_id": folder.parent_id,
                    },
                }

            return {
                "error": (
                    "manage_library_folders: 'action' must be 'create', "
                    "'move', or 'rename'."
                ),
                "exit_code": 1,
            }
        except Exception as exc:
            db.rollback()
            return {
                "error": f"manage_library_folders: {exc}",
                "exit_code": 1,
            }
        finally:
            db.close()
