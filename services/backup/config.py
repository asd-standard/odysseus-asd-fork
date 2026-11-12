"""Configuration and persisted state for Odysseus full-instance backups.

The admin console owns the backup folder, interval and retention.  The folder
must live below one of the mounted backup roots (``ODYSSEUS_BACKUP_ROOTS``);
the app container cannot write to an arbitrary host path.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.platform_compat import safe_chmod
from src.constants import DATA_DIR

SETTINGS_FILE = os.path.join(DATA_DIR, "backup_settings.json")
STATE_FILE = os.path.join(DATA_DIR, "backup_state.json")

DEFAULT_INTERVAL_HOURS = 8
DEFAULT_RETENTION = 21
DEFAULT_ROOT = "/app/backups"

_SETTING_KEYS = {
    "enabled",
    "directory",
    "interval_hours",
    "retention",
    "include_attachments",
    "include_research",
    "notify_on_failure",
}

DEFAULT_STATE: Dict[str, Any] = {
    "last_run": None,
    "last_success": None,
    "last_error": None,
    "last_file": None,
    "last_size": 0,
    "last_duration_seconds": None,
    "last_download_at": None,
    "last_download_file": None,
    "running": False,
}


def backup_roots() -> List[str]:
    """Return the backup roots mounted into the container."""
    raw = os.getenv("ODYSSEUS_BACKUP_ROOTS", DEFAULT_ROOT) or DEFAULT_ROOT
    roots: List[str] = []
    for part in raw.replace(";", os.pathsep).split(os.pathsep):
        path = part.strip()
        if not path:
            continue
        path = os.path.abspath(os.path.expanduser(path))
        if path not in roots:
            roots.append(path)
    return roots or [DEFAULT_ROOT]


def host_root_map() -> Dict[str, str]:
    """Map container backup roots to their host paths for display/restore.

    The host path is provided by ``ODYSSEUS_BACKUP_HOST_DIR``.  When one host
    path is configured it maps to the first root; a pathsep-separated list maps
    positionally to the roots.
    """
    roots = backup_roots()
    raw = os.getenv("ODYSSEUS_BACKUP_HOST_DIR", "").strip()
    if not raw:
        return {}
    hosts = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
    if len(hosts) == 1:
        return {roots[0]: hosts[0]}
    return {root: host for root, host in zip(roots, hosts)}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _clean_settings(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    roots = backup_roots()
    default_dir = roots[0]
    try:
        interval = int(data.get("interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_HOURS
    interval = max(1, min(168, interval))
    try:
        retention = int(data.get("retention", DEFAULT_RETENTION))
    except (TypeError, ValueError):
        retention = DEFAULT_RETENTION
    retention = max(1, min(365, retention))
    directory = str(data.get("directory") or default_dir).strip() or default_dir
    return {
        "enabled": _as_bool(data.get("enabled"), False),
        "directory": directory,
        "interval_hours": interval,
        "retention": retention,
        "include_attachments": _as_bool(data.get("include_attachments"), True),
        "include_research": _as_bool(data.get("include_research"), True),
        "notify_on_failure": _as_bool(data.get("notify_on_failure"), True),
    }


def load_settings() -> Dict[str, Any]:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            return _clean_settings(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _clean_settings({})


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = _clean_settings(settings)
    atomic_write_json(SETTINGS_FILE, cleaned, indent=2)
    safe_chmod(SETTINGS_FILE, 0o600)
    return cleaned


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = {}
    state = dict(DEFAULT_STATE)
    if isinstance(raw, dict):
        for key in DEFAULT_STATE:
            if key in raw:
                state[key] = raw[key]
    return state


def save_state(state: Dict[str, Any]) -> None:
    data = dict(DEFAULT_STATE)
    if isinstance(state, dict):
        for key in DEFAULT_STATE:
            if key in state:
                data[key] = state[key]
    atomic_write_json(STATE_FILE, data, indent=2)
    safe_chmod(STATE_FILE, 0o600)


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def validate_directory(directory: str) -> str:
    """Validate/create a backup folder and return its real path.

    The folder must be absolute, below one of the mounted roots, writable, and
    outside the application's data directory (otherwise the backup would
    include itself).
    """
    directory = str(directory or "").strip()
    if not directory:
        raise ValueError("Backup folder is required")
    if not os.path.isabs(directory):
        raise ValueError("Backup folder must be an absolute path")

    real = _real(directory)
    roots = [_real(root) for root in backup_roots()]
    matched_root = next(
        (root for root in roots if real == root or real.startswith(root + os.sep)),
        None,
    )
    if matched_root is None:
        raise ValueError(
            "Backup folder must be inside a mounted backup root: "
            + ", ".join(backup_roots())
        )
    if not os.path.isdir(matched_root):
        raise ValueError(
            "Backup root is not mounted: "
            + matched_root
            + ". Set APP_BACKUP_DIR on the host and restart the container."
        )

    data_real = _real(DATA_DIR)
    if real == data_real or real.startswith(data_real + os.sep):
        raise ValueError("Backup folder cannot be inside the Odysseus data directory")

    if not os.path.isdir(real):
        parent = os.path.dirname(real)
        if not os.path.isdir(parent):
            raise ValueError(
                "Backup folder does not exist and its parent is not available. "
                "Create the parent folder or use the mounted backup root."
            )
        os.makedirs(real, mode=0o700, exist_ok=True)

    if not os.access(real, os.W_OK | os.X_OK):
        raise ValueError("Backup folder is not writable by the Odysseus process")

    return real


def root_info() -> List[Dict[str, Any]]:
    host_map = host_root_map()
    result: List[Dict[str, Any]] = []
    for root in backup_roots():
        real = _real(root)
        exists = os.path.isdir(real)
        writable = exists and os.access(real, os.W_OK | os.X_OK)
        free_bytes = None
        total_bytes = None
        if exists:
            try:
                usage = shutil.disk_usage(real)
                free_bytes = usage.free
                total_bytes = usage.total
            except OSError:
                pass
        result.append(
            {
                "path": real,
                "host_path": host_map.get(root) or host_map.get(real) or "",
                "exists": exists,
                "writable": writable,
                "free_bytes": free_bytes,
                "total_bytes": total_bytes,
            }
        )
    return result


def host_directory_for(directory: str) -> str:
    """Best-effort host path for a container directory, for the UI/restore hint."""
    real = _real(directory)
    host_map = host_root_map()
    for root, host in host_map.items():
        root_real = _real(root)
        if real == root_real:
            return host
        if real.startswith(root_real + os.sep):
            rel = os.path.relpath(real, root_real)
            return os.path.join(host, rel)
    return ""


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def next_run_iso(settings: Dict[str, Any], state: Dict[str, Any]) -> Optional[str]:
    if not settings.get("enabled"):
        return None
    interval = int(settings.get("interval_hours", DEFAULT_INTERVAL_HOURS))
    last = _parse_time(state.get("last_success"))
    if last is None:
        return datetime.now(timezone.utc).isoformat()
    return (last + timedelta(hours=interval)).isoformat()
