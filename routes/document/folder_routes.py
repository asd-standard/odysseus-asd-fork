"""Folder routes — virtual, owner-scoped, nested folders for the document library.

Folders are rows in the ``folders`` table (see ``core.database.Folder``) and
documents reference them via ``Document.folder_id``. Nothing here touches the
filesystem: it is a logical hierarchy layered over the existing SQLite-backed
document library.
"""

import uuid
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sqlalchemy import func

from core.database import SessionLocal, Document, Folder, utcnow_naive
from src.auth_helpers import get_current_user
from routes.document.document_helpers import _folder_owner_filter, _verify_folder_owner

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 120


class FolderCreate(BaseModel):
    name: str
    parent_id: Optional[str] = None


class FolderRename(BaseModel):
    name: str


class FolderMove(BaseModel):
    parent_id: Optional[str] = None  # None = move to root


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Folder name is required")
    if len(name) > MAX_NAME_LENGTH:
        raise HTTPException(400, f"Folder name must be {MAX_NAME_LENGTH} characters or fewer")
    return name


def _get_owned_folder(db, user: Optional[str], folder_id: str) -> Folder:
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(404, "Folder not found")
    _verify_folder_owner(db, folder, user)
    return folder


def _assert_no_sibling_name(db, user: Optional[str], parent_id: Optional[str], name: str, exclude_id: Optional[str] = None):
    """Reject a folder whose sibling (same parent, same owner) already uses `name`."""
    q = db.query(Folder).filter(Folder.name == name)
    if parent_id is None:
        q = q.filter(Folder.parent_id.is_(None))
    else:
        q = q.filter(Folder.parent_id == parent_id)
    q = _folder_owner_filter(q, user)
    if exclude_id:
        q = q.filter(Folder.id != exclude_id)
    if q.first():
        raise HTTPException(409, f"A folder named '{name}' already exists here")


def _descendant_ids(folders: List[Folder], root_id: str) -> set:
    """Return the set of ids strictly below `root_id` (its children and all descendants)."""
    children: Dict[Optional[str], List[str]] = {}
    for f in folders:
        children.setdefault(f.parent_id, []).append(f.id)
    seen = set()
    stack = list(children.get(root_id, []))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))
    return seen


def _build_tree(folders: List[Folder], doc_counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """Assemble the nested folder tree from a flat folder list."""
    nodes = {
        f.id: {
            "id": f.id,
            "name": f.name,
            "parent_id": f.parent_id,
            "doc_count": doc_counts.get(f.id, 0),
            "children": [],
        }
        for f in folders
    }
    roots = []
    for f in folders:
        node = nodes[f.id]
        parent = nodes.get(f.parent_id) if f.parent_id else None
        if parent is not None:
            parent["children"].append(node)
        else:
            roots.append(node)

    def sort_nodes(n):
        n["children"].sort(key=lambda c: (c["name"].lower(), c["id"]))
        for c in n["children"]:
            sort_nodes(c)

    roots.sort(key=lambda n: (n["name"].lower(), n["id"]))
    for r in roots:
        sort_nodes(r)
    return roots


def setup_folder_routes() -> APIRouter:
    router = APIRouter(tags=["folders"])

    # ---- GET /api/folders — folder tree + counts ----
    @router.get("/api/folders")
    async def list_folders(request: Request) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            folders = _folder_owner_filter(db.query(Folder), user).all()

            # Direct doc counts per folder (owner-filtered, active, not archived).
            count_q = (
                db.query(Document.folder_id, func.count(Document.id))
                .filter(Document.is_active == True)
                .filter(Document.folder_id.isnot(None))
            )
            from sqlalchemy import or_
            count_q = count_q.filter(or_(Document.archived == False, Document.archived.is_(None)))
            if user:
                count_q = count_q.filter(Document.owner == user)
            doc_counts = {fid: cnt for fid, cnt in count_q.group_by(Document.folder_id).all()}

            # Unfiled (root) doc count for the sidebar's "Unfiled" node.
            unfiled_q = (
                db.query(func.count(Document.id))
                .filter(Document.is_active == True)
                .filter(Document.folder_id.is_(None))
                .filter(or_(Document.archived == False, Document.archived.is_(None)))
            )
            if user:
                unfiled_q = unfiled_q.filter(Document.owner == user)
            unfiled_count = unfiled_q.scalar() or 0

            return {
                "folders": _build_tree(folders, doc_counts),
                "unfiled_count": unfiled_count,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to fetch folders: {e}")
            raise HTTPException(500, f"Failed to fetch folders: {e}")
        finally:
            db.close()

    # ---- POST /api/folders — create ----
    @router.post("/api/folders")
    async def create_folder(request: Request, req: FolderCreate) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            name = _clean_name(req.name)
            parent = None
            if req.parent_id:
                parent = _get_owned_folder(db, user, req.parent_id)
            _assert_no_sibling_name(db, user, parent.id if parent else None, name)
            folder = Folder(
                id=str(uuid.uuid4()),
                name=name,
                parent_id=parent.id if parent else None,
                owner=user,
                sort_order=0,
            )
            db.add(folder)
            db.commit()
            db.refresh(folder)
            return {
                "id": folder.id,
                "name": folder.name,
                "parent_id": folder.parent_id,
                "doc_count": 0,
                "children": [],
            }
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to create folder: {e}")
            raise HTTPException(500, f"Failed to create folder: {e}")
        finally:
            db.close()

    # ---- PATCH /api/folders/{folder_id} — rename ----
    @router.patch("/api/folders/{folder_id}")
    async def rename_folder(request: Request, folder_id: str, req: FolderRename) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            folder = _get_owned_folder(db, user, folder_id)
            name = _clean_name(req.name)
            _assert_no_sibling_name(db, user, folder.parent_id, name, exclude_id=folder.id)
            folder.name = name
            db.commit()
            return {"id": folder.id, "name": folder.name, "parent_id": folder.parent_id}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to rename folder: {e}")
            raise HTTPException(500, f"Failed to rename folder: {e}")
        finally:
            db.close()

    # ---- PATCH /api/folders/{folder_id}/move — nest under another folder / root ----
    @router.patch("/api/folders/{folder_id}/move")
    async def move_folder(request: Request, folder_id: str, req: FolderMove) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            folder = _get_owned_folder(db, user, folder_id)
            new_parent_id = req.parent_id
            if new_parent_id == folder.id:
                raise HTTPException(400, "A folder cannot be moved into itself")
            if new_parent_id:
                new_parent = _get_owned_folder(db, user, new_parent_id)
                new_parent_id = new_parent.id
                # Cycle guard: `new_parent` must not be a descendant of `folder`.
                all_folders = _folder_owner_filter(db.query(Folder), user).all()
                if new_parent_id in _descendant_ids(all_folders, folder.id):
                    raise HTTPException(400, "A folder cannot be moved into its own subfolder")
            _assert_no_sibling_name(db, user, new_parent_id, folder.name, exclude_id=folder.id)
            folder.parent_id = new_parent_id
            db.commit()
            return {"id": folder.id, "parent_id": folder.parent_id}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to move folder: {e}")
            raise HTTPException(500, f"Failed to move folder: {e}")
        finally:
            db.close()

    # ---- DELETE /api/folders/{folder_id} — recursive delete ----
    @router.delete("/api/folders/{folder_id}")
    async def delete_folder(request: Request, folder_id: str) -> Dict[str, bool]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            folder = _get_owned_folder(db, user, folder_id)

            # Recursively collect the folder + all its descendants.
            all_folders = _folder_owner_filter(db.query(Folder), user).all()
            folder_ids = {folder.id} | _descendant_ids(all_folders, folder.id)

            # Move documents filed anywhere in the subtree to Trash. Restoring
            # them later returns them to Unfiled (the folder tree is gone).
            doc_q = db.query(Document).filter(
                Document.folder_id.in_(folder_ids),
                Document.is_active == True,
            )
            if user:
                doc_q = doc_q.filter(Document.owner == user)
            doc_q.update(
                {
                    "is_active": False,
                    "deleted_at": utcnow_naive(),
                    "folder_id": None,
                },
                synchronize_session=False,
            )

            # Hard-delete the folders. Bulk delete bypasses ORM relationship
            # handling so the parent_id ON DELETE CASCADE is never relied upon
            # (the whole subtree is removed in one statement).
            db.query(Folder).filter(Folder.id.in_(folder_ids)).delete(synchronize_session=False)

            db.commit()
            return {"ok": True}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to delete folder: {e}")
            raise HTTPException(500, f"Failed to delete folder: {e}")
        finally:
            db.close()

    return router
