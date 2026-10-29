"""Narrow reader for files attached to the current chat.

The agent has no filesystem tools on this deployment. Uploaded files are still
useful when their inline text was truncated, so this tool resolves an upload id
through the upload handler (never an arbitrary path) and returns its text.
Images are rejected: image support is disabled.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

MAX_READ_CHARS = 20_000


def _parse_args(content: str) -> Dict:
    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return {"id": raw.split("\n")[0].strip()} if raw else {}


class ReadUploadTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        args = _parse_args(content)
        upload_id = str(args.get("id") or args.get("upload_id") or "").strip()
        if not upload_id:
            return {"error": "read_upload: id is required", "exit_code": 1}

        owner = ctx.get("owner")
        handler = ctx.get("upload_handler")
        if handler is None:
            try:
                from src.tool_utils import get_upload_handler

                handler = get_upload_handler()
            except Exception:
                handler = None
        info: Optional[Dict] = None
        if handler is not None and hasattr(handler, "resolve_upload"):
            try:
                info = handler.resolve_upload(upload_id, owner=owner)
            except Exception as exc:
                logger.warning("read_upload: resolve failed for %s: %s", upload_id, exc)
        if info is None:
            return {"error": f"read_upload: upload '{upload_id}' not found", "exit_code": 1}

        path = info.get("path") or ""
        name = info.get("name") or info.get("original_name") or upload_id
        mime = info.get("mime") or mimetypes.guess_type(path)[0] or ""
        if mime.startswith("image/") or (
            handler is not None
            and getattr(handler, "is_image_file", None)
            and handler.is_image_file(name, mime)
        ):
            return {"error": "read_upload: image attachments are not supported", "exit_code": 1}
        if not path or not os.path.isfile(path):
            return {"error": "read_upload: upload file is missing", "exit_code": 1}

        try:
            limit = max(1, min(int(args.get("limit", MAX_READ_CHARS)), MAX_READ_CHARS))
        except (TypeError, ValueError):
            limit = MAX_READ_CHARS
        try:
            offset = max(0, int(args.get("offset", 0) or 0))
        except (TypeError, ValueError):
            offset = 0

        try:
            if mime == "application/pdf" or str(name).lower().endswith(".pdf"):
                from src.document_processor import _process_pdf

                text = _process_pdf(path, owner=owner, max_chars=None)
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
        except Exception as exc:
            logger.warning("read_upload: failed to read %s: %s", upload_id, exc)
            return {"error": f"read_upload: {exc}", "exit_code": 1}

        offset = min(offset, len(text))
        end = min(offset + limit, len(text))
        chunk = text[offset:end]
        truncated = end < len(text)
        if truncated:
            chunk += f"\n... (truncated, {len(text)} chars total; next_offset={end})"
        return {
            "output": chunk,
            "upload_id": upload_id,
            "name": name,
            "mime": mime,
            "size": len(text),
            "offset": offset,
            "next_offset": end if truncated else None,
            "exit_code": 0,
        }
