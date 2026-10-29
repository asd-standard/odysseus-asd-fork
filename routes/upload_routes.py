# routes/upload_routes.py
import os
import time
import json
import asyncio
import shutil
import uuid
from pathlib import Path
from fastapi import APIRouter, Request, File, UploadFile, HTTPException, Form
from typing import List, Optional
import logging
from core.middleware import require_admin
from core.database import (
    SessionLocal,
    ChatMessage as DbChatMessage,
    CalendarCal,
    CalendarEvent,
    Document,
    DocumentVersion,
    Note,
    Session as DbSession,
)
from src.auth_helpers import effective_user
from src.attachment_refs import attachment_refs_from_metadata
from src.upload_handler import (
    UploadCleanupSafetyError,
    count_recent_uploads,
    extract_upload_ids,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upload", tags=["upload"])
UPLOAD_RESPONSE_HEADERS = {"X-Content-Type-Options": "nosniff"}

def _upload_ids_from_persisted_text(value: object) -> set[str]:
    """Return canonical upload IDs embedded in persisted text.

    This covers attachment reference lines/URIs and the PDF source markers
    stored by the document editor. False positives are intentionally
    conservative: retaining an extra upload is safer than deleting referenced
    bytes.
    """
    return extract_upload_ids(value)


def _upload_ids_from_message_metadata(raw_metadata: object) -> set[str]:
    """Extract attachment IDs from a persisted chat metadata JSON value.

    Malformed metadata raises instead of being treated as an empty reference
    set. The admin cleanup route catches that failure and aborts cleanup.
    """
    if raw_metadata in (None, ""):
        return set()
    if isinstance(raw_metadata, str):
        metadata = json.loads(raw_metadata)
    else:
        metadata = raw_metadata
    if not isinstance(metadata, dict):
        raise ValueError("chat message metadata must be a JSON object")

    attachments = metadata.get("attachments")
    if attachments is not None:
        if not isinstance(attachments, list) or any(
            not isinstance(item, dict) for item in attachments
        ):
            raise ValueError("chat message attachments metadata is malformed")

    ids = {
        str(ref["attachment_id"])
        for ref in attachment_refs_from_metadata(metadata)
        if ref.get("attachment_id")
    }
    # Preserve canonical IDs even in older metadata shapes not normalized by
    # attachment_refs_from_metadata().
    ids.update(_upload_ids_from_persisted_text(json.dumps(metadata)))
    return ids


def _collect_persisted_upload_references() -> tuple[set[str], set[str]]:
    """Collect upload IDs/hashes still referenced by durable application data.

    The caller must treat any exception as an incomplete scan and fail closed.
    There is no distinct artifact table in the current schema; artifact-like
    attachment references persisted in chat/document text are covered by the
    canonical-ID scan.
    """
    referenced_ids: set[str] = set()
    referenced_hashes: set[str] = set()
    db = SessionLocal()
    try:
        for content, raw_metadata in db.query(
            DbChatMessage.content,
            DbChatMessage.meta_data,
        ).yield_per(500):
            referenced_ids.update(_upload_ids_from_persisted_text(content))
            referenced_ids.update(_upload_ids_from_message_metadata(raw_metadata))

        for (content,) in db.query(Document.current_content).yield_per(500):
            referenced_ids.update(_upload_ids_from_persisted_text(content))

        for (content,) in db.query(DocumentVersion.content).yield_per(500):
            referenced_ids.update(_upload_ids_from_persisted_text(content))

        for image_url, color, content, items in db.query(
            Note.image_url,
            Note.color,
            Note.content,
            Note.items,
        ).yield_per(500):
            for value in (image_url, color, content, items):
                referenced_ids.update(_upload_ids_from_persisted_text(value))

        for (color,) in db.query(CalendarCal.color).yield_per(500):
            referenced_ids.update(_upload_ids_from_persisted_text(color))

        for color, description, location in db.query(
            CalendarEvent.color,
            CalendarEvent.description,
            CalendarEvent.location,
        ).yield_per(500):
            for value in (color, description, location):
                referenced_ids.update(_upload_ids_from_persisted_text(value))

        return referenced_ids, referenced_hashes
    finally:
        db.close()


def _run_reference_safe_cleanup(upload_handler) -> int:
    referenced_ids, referenced_hashes = _collect_persisted_upload_references()
    return upload_handler.cleanup_old_uploads(
        referenced_upload_ids=referenced_ids,
        referenced_upload_hashes=referenced_hashes,
    )

def setup_upload_routes(upload_handler):
    """Setup upload routes with the provided handler"""

    def _upload_root() -> str:
        from src.constants import UPLOAD_DIR
        return os.path.realpath(getattr(upload_handler, "upload_dir", UPLOAD_DIR))

    def _path_inside_upload_dir(path: str) -> bool:
        try:
            return os.path.commonpath([_upload_root(), os.path.realpath(path)]) == _upload_root()
        except Exception:
            return False

    def _resolve_upload_path(file_id: str) -> str:
        from src.constants import UPLOAD_DIR
        upload_root = getattr(upload_handler, "upload_dir", UPLOAD_DIR)
        direct = os.path.join(upload_root, file_id)
        if os.path.lexists(direct):
            if not _path_inside_upload_dir(direct):
                raise HTTPException(403, "Access denied")
            if os.path.isfile(direct):
                return direct
            raise HTTPException(404, "File not found")

        for root, _dirs, files in os.walk(upload_root, followlinks=False):
            if file_id not in files:
                continue
            path = os.path.join(root, file_id)
            if not _path_inside_upload_dir(path):
                raise HTTPException(403, "Access denied")
            if os.path.isfile(path):
                return path
            raise HTTPException(404, "File not found")

        raise HTTPException(404, "File not found")

    def _valid_session_id_for_owner(db, session_id: str | None, owner: str | None) -> str | None:
        if not session_id:
            return None
        sess = db.query(DbSession).filter(DbSession.id == session_id).first()
        if not sess:
            return None
        if owner and sess.owner and sess.owner != owner:
            return None
        return session_id

    @router.post("")
    async def api_upload(
        request: Request,
        files: List[UploadFile] = File(...),
        session_id: Optional[str] = Form(None),
    ):
        """Upload files with enhanced security and organization."""
        if not isinstance(session_id, str):
            session_id = None
        if not files:
            raise HTTPException(400, "No files uploaded")
            
        client_ip = request.client.host if request.client else "unknown"
        out = []

        # Limit concurrent uploads per IP. Count genuine recent upload events —
        # NOT the number of files in this batch. The previous check summed over
        # `files`, so a single multi-file request counted itself as N concurrent
        # uploads and tripped the limit (issue #1346: "attach more than one file
        # → the model doesn't even see them"). save_upload still enforces the
        # per-minute sliding-window rate limit per file.
        recent_uploads = count_recent_uploads(
            upload_handler.upload_rate_log.get(client_ip, []), time.time()
        )

        if recent_uploads >= upload_handler.max_concurrent_uploads:
            raise HTTPException(
                status_code=429,
                detail=f"Maximum concurrent uploads ({upload_handler.max_concurrent_uploads}) exceeded"
            )
        
        for u in files:
            try:
                owner = effective_user(request)
                _is_image = getattr(upload_handler, "is_image_file", None)
                if callable(_is_image) and _is_image(u.filename or "", u.content_type or ""):
                    raise HTTPException(
                        400,
                        "Image uploads are not supported on this deployment.",
                    )
                meta = upload_handler.save_upload(u, client_ip, owner=owner)
                item = {
                    "id": meta["id"],
                    "name": meta["name"],
                    "mime": meta["mime"],
                    "size": meta["size"],
                    "hash": meta["hash"],
                    "checksum_sha256": meta.get("checksum_sha256") or meta["hash"],
                    "uploaded_at": meta["uploaded_at"],
                    "created_at": meta.get("created_at") or meta["uploaded_at"],
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                    "is_duplicate": meta.get("is_duplicate", False)
                }
                out.append(item)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Failed to process upload {u.filename}: {str(e)}")
                continue
        
        if not out:
            raise HTTPException(500, "All file uploads failed")
            
        return {"files": out}
    
    @router.post("/cleanup")
    async def manual_cleanup(request: Request):
        """Manually trigger cleanup of old uploads."""
        require_admin(request)
        try:
            cleaned_count = await asyncio.to_thread(
                _run_reference_safe_cleanup,
                upload_handler,
            )
        except UploadCleanupSafetyError:
            logger.exception("Upload cleanup aborted because index safety checks failed")
            raise HTTPException(
                503,
                "Upload cleanup aborted because upload index integrity could not be verified",
            )
        except Exception:
            logger.exception("Upload cleanup skipped because reference discovery failed")
            raise HTTPException(
                503,
                "Upload cleanup skipped because persisted references could not be verified",
            )
        return {"status": "success", "files_cleaned": cleaned_count}

    @router.get("/stats")
    async def upload_stats(request: Request):
        """Get statistics about uploaded files."""
        require_admin(request)
        try:
            return upload_handler.get_upload_stats()
        except Exception as e:
            logger.error(f"Failed to get upload stats: {e}")
            raise HTTPException(500, "Failed to get upload statistics")

    @router.get("/{file_id}")
    async def download_file(request: Request, file_id: str, thumb: int = 0):
        """Serve an uploaded non-image file by its ID."""
        if not upload_handler.validate_upload_id(file_id):
            raise HTTPException(400, "Invalid file ID")
        import mimetypes as _mt
        # Look up original filename and owner from uploads.json
        original_name = file_id
        # _load_upload_index() tolerates a missing/corrupt uploads.json (it falls
        # back to the .bak sibling, then to {}), so a truncated DB degrades to
        # "no metadata" instead of a 500 from an unhandled JSONDecodeError.
        db = upload_handler._load_upload_index()
        info = next((fi for fi in db.values() if fi.get("id") == file_id), None)
        if info:
            original_name = info.get("name", file_id)
        auth_mgr = getattr(request.app.state, "auth_manager", None)
        auth_configured = bool(auth_mgr and auth_mgr.is_configured)
        current_user = effective_user(request)
        file_owner = info.get("owner") if info else None
        if auth_configured:
            if not current_user:
                raise HTTPException(403, "Access denied")
            if file_owner != current_user and not auth_mgr.is_admin(current_user):
                raise HTTPException(404, "File not found")
        path = _resolve_upload_path(file_id)
        mime = (info or {}).get("mime") or _mt.guess_type(path)[0] or "application/octet-stream"
        from fastapi.responses import FileResponse
        return FileResponse(
            path,
            media_type=mime,
            filename=original_name,
            headers=UPLOAD_RESPONSE_HEADERS,
        )

    def _load_upload_info(file_id: str):
        """Look up the uploads.json record for a file_id, with owner/auth checks."""
        # Corruption-tolerant load: a bad uploads.json yields {} rather than
        # raising JSONDecodeError from upload lookups.
        db = upload_handler._load_upload_index()
        return next((fi for fi in db.values() if fi.get("id") == file_id), None)

    async def periodic_rate_limit_cleanup():
        """Background task to run cleanup every hour"""
        while True:
            await asyncio.sleep(3600)
            upload_handler.cleanup_rate_limits()
    
    return router, periodic_rate_limit_cleanup
