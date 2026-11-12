"""Full-instance backup service for the admin console.

The archive format is intentionally simple and self-describing:

    odysseus-full-<auto|manual>-YYYYMMDD-HHMMSS.tar.gz
      manifest.json
      data/...            (consistent SQLite copies + user files)

A sidecar ``.sha256`` and ``.manifest.json`` live next to each archive so the
admin UI can list/download without opening the archive.  Restore is a host-side
operation (``scripts/odysseus-restore``) because the app cannot safely replace
its own live database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.platform_compat import safe_chmod
from src.constants import APP_VERSION, DATA_DIR

from . import config


class BackupError(Exception):
    """A backup could not be created or read."""


class BackupBusy(BackupError):
    """Another backup is currently running."""


# Directories that are caches/model downloads/derived indexes.  They are
# intentionally excluded from the full archive; a restore rebuilds them.
_EXCLUDED_DIRS = {
    "__pycache__",
    "backups",
    "cache",
    "chroma",
    "email_urgency_cache",
    "emoji_cache",
    "fastembed_cache",
    "huggingface",
    "hwfit",
    "local",
    "logs",
    "memory_vectors",
    "rag",
    "tts_cache",
}

_ARCHIVE_RE = re.compile(
    r"^odysseus-full-(?P<kind>auto|manual)-(?P<stamp>\d{8}-\d{6})(?P<suffix>-\d+)?\.tar\.gz$"
)
_SECRET_NAMES = {
    ".app_key",
    "auth.json",
    "sessions.json",
    "backup_settings.json",
    "backup_state.json",
}

_COUNT_TABLES = (
    "sessions",
    "chat_messages",
    "documents",
    "document_versions",
    "notes",
    "scheduled_tasks",
    "task_runs",
    "gallery_images",
    "gallery_albums",
    "memories",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    return f"{value / (1024 * 1024 * 1024):.2f} GiB"


class BackupService:
    """Create, list, download and rotate full-instance archives."""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        *,
        app_version: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.data_dir = os.path.abspath(data_dir or DATA_DIR)
        self.app_version = app_version or APP_VERSION
        self.logger = logger or logging.getLogger("services.backup")
        self._lock = threading.Lock()

    # ── public API ──────────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        settings = config.load_settings()
        state = config.load_state()
        directory = ""
        directory_error = ""
        free_bytes = None
        total_bytes = None
        try:
            directory = config.validate_directory(settings["directory"])
            usage = shutil.disk_usage(directory)
            free_bytes = usage.free
            total_bytes = usage.total
        except Exception as exc:
            directory_error = str(exc)

        restore_command = os.getenv(
            "ODYSSEUS_RESTORE_COMMAND",
            "sudo /home/ldr/odysseus/app/scripts/odysseus-restore --latest",
        )
        return {
            "settings": settings,
            "state": state,
            "roots": config.root_info(),
            "directory": directory or settings["directory"],
            "directory_error": directory_error,
            "directory_writable": bool(directory),
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "free_display": _format_bytes(free_bytes) if free_bytes is not None else "",
            "host_directory": config.host_directory_for(directory) if directory else "",
            "next_run": config.next_run_iso(settings, state),
            "restore_command": restore_command,
        }

    def create_backup(self, reason: str = "manual") -> Dict[str, Any]:
        """Create a full backup.  Raises BackupBusy / BackupError."""
        reason = "scheduled" if reason == "scheduled" else "manual"
        kind = "auto" if reason == "scheduled" else "manual"

        if not self._lock.acquire(blocking=False):
            raise BackupBusy("A backup is already running")

        settings = config.load_settings()
        state = config.load_state()
        stage_dir: Optional[str] = None
        tmp_archive: Optional[str] = None
        started_monotonic = time.monotonic()
        try:
            if reason == "scheduled" and not settings.get("enabled"):
                raise BackupError("Scheduled backups are disabled")

            directory = config.validate_directory(settings["directory"])
            source_bytes = self._estimate_source_size(settings)
            free_bytes = shutil.disk_usage(directory).free
            required = max(64 * 1024 * 1024, int(source_bytes * 1.25))
            if free_bytes < required:
                raise BackupError(
                    "Not enough free space in the backup folder "
                    f"({_format_bytes(free_bytes)} free, need about "
                    f"{_format_bytes(required)})."
                )

            state.update(
                {
                    "last_run": _utcnow(),
                    "last_error": None,
                    "running": True,
                }
            )
            config.save_state(state)

            stage_dir = tempfile.mkdtemp(prefix=".odysseus-backup-", dir=directory)
            stage_data = os.path.join(stage_dir, "data")
            os.makedirs(stage_data, mode=0o700, exist_ok=True)
            file_count, file_bytes = self._copy_data(stage_data, settings)

            manifest: Dict[str, Any] = {
                "format": "odysseus-full-backup",
                "version": 1,
                "created_at": _utcnow(),
                "reason": reason,
                "app_version": self.app_version,
                "hostname": socket.gethostname(),
                "data_dir": self.data_dir,
                "file_count": file_count,
                "file_bytes": file_bytes,
                "file_bytes_display": _format_bytes(file_bytes),
                "excluded_dirs": sorted(_EXCLUDED_DIRS),
                "include_attachments": bool(settings.get("include_attachments", True)),
                "include_research": bool(settings.get("include_research", True)),
                "counts": self._manifest_counts(stage_data),
            }
            manifest_path = os.path.join(stage_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, ensure_ascii=False)

            tmp_archive = os.path.join(
                directory, f".odysseus-backup-{uuid.uuid4().hex}.tar.gz"
            )
            with tarfile.open(tmp_archive, "w:gz", compresslevel=6) as archive:
                archive.add(manifest_path, arcname="manifest.json")
                archive.add(stage_data, arcname="data", recursive=True)

            digest = _sha256_file(tmp_archive)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base = f"odysseus-full-{kind}-{stamp}"
            name = f"{base}.tar.gz"
            counter = 1
            while os.path.exists(os.path.join(directory, name)):
                counter += 1
                name = f"{base}-{counter}.tar.gz"
            final_path = os.path.join(directory, name)
            os.replace(tmp_archive, final_path)
            tmp_archive = None
            safe_chmod(final_path, 0o600)

            manifest["archive"] = name
            manifest["sha256"] = digest
            manifest["size"] = os.path.getsize(final_path)
            manifest["size_display"] = _format_bytes(manifest["size"])
            manifest["duration_seconds"] = round(time.monotonic() - started_monotonic, 2)

            sha_path = final_path + ".sha256"
            with open(sha_path, "w", encoding="utf-8") as handle:
                handle.write(f"{digest}  {name}\n")
            safe_chmod(sha_path, 0o600)

            sidecar_path = final_path + ".manifest.json"
            with open(sidecar_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, ensure_ascii=False)
            safe_chmod(sidecar_path, 0o600)

            pruned = self._prune(directory, int(settings.get("retention", 21)), keep=name)
            state.update(
                {
                    "last_success": _utcnow(),
                    "last_error": None,
                    "last_file": name,
                    "last_size": manifest["size"],
                    "last_duration_seconds": manifest["duration_seconds"],
                    "running": False,
                }
            )
            config.save_state(state)
            self.logger.info(
                "Full backup created: %s (%s, %s, pruned=%d)",
                name,
                manifest["size_display"],
                manifest["duration_seconds"],
                pruned,
            )
            return {
                "name": name,
                "path": final_path,
                "sha256": digest,
                "size": manifest["size"],
                "size_display": manifest["size_display"],
                "created_at": manifest["created_at"],
                "reason": reason,
                "duration_seconds": manifest["duration_seconds"],
                "manifest": manifest,
                "pruned": pruned,
            }
        except BackupError as exc:
            self._record_failure(exc, settings)
            raise
        except Exception as exc:
            self._record_failure(exc, settings)
            raise BackupError(str(exc)) from exc
        finally:
            state = config.load_state()
            state["running"] = False
            config.save_state(state)
            if stage_dir:
                shutil.rmtree(stage_dir, ignore_errors=True)
            if tmp_archive and os.path.exists(tmp_archive):
                try:
                    os.unlink(tmp_archive)
                except OSError:
                    pass
            self._lock.release()

    def list_backups(self) -> List[Dict[str, Any]]:
        settings = config.load_settings()
        directory = config.validate_directory(settings["directory"])
        result: List[Dict[str, Any]] = []
        try:
            names = os.listdir(directory)
        except OSError as exc:
            raise BackupError(f"Cannot read backup folder: {exc}") from exc

        for name in names:
            if not _ARCHIVE_RE.match(name):
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            manifest = self._read_sidecar(path)
            sha256 = self._read_sha(path)
            kind = _ARCHIVE_RE.match(name).group("kind")
            created_at = manifest.get("created_at")
            if not created_at:
                created_at = datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat()
            result.append(
                {
                    "name": name,
                    "size": stat.st_size,
                    "size_display": _format_bytes(stat.st_size),
                    "created_at": created_at,
                    "mtime": stat.st_mtime,
                    "reason": manifest.get("reason") or (
                        "scheduled" if kind == "auto" else "manual"
                    ),
                    "app_version": manifest.get("app_version", ""),
                    "sha256": sha256,
                    "manifest": manifest,
                }
            )
        result.sort(key=lambda item: item.get("mtime", 0), reverse=True)
        return result

    def resolve_backup(self, name: str) -> str:
        if not isinstance(name, str) or os.path.basename(name) != name:
            raise BackupError("Invalid backup name")
        if not _ARCHIVE_RE.match(name):
            raise BackupError("Invalid backup name")
        settings = config.load_settings()
        directory = config.validate_directory(settings["directory"])
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            raise BackupError("Backup not found")
        return path

    def delete_backup(self, name: str) -> bool:
        path = self.resolve_backup(name)
        for candidate in (path, path + ".sha256", path + ".manifest.json"):
            try:
                if os.path.isfile(candidate):
                    os.unlink(candidate)
            except OSError as exc:
                raise BackupError(f"Could not delete {os.path.basename(candidate)}: {exc}") from exc
        state = config.load_state()
        if state.get("last_file") == name:
            state["last_file"] = None
        config.save_state(state)
        self.logger.info("Full backup deleted: %s", name)
        return True

    def mark_downloaded(self, name: str) -> None:
        state = config.load_state()
        state["last_download_at"] = _utcnow()
        state["last_download_file"] = name
        config.save_state(state)

    # ── internals ───────────────────────────────────────────────────────
    def _record_failure(self, exc: Exception, settings: Dict[str, Any]) -> None:
        state = config.load_state()
        state.update(
            {
                "last_error": f"{type(exc).__name__}: {exc}",
                "running": False,
            }
        )
        config.save_state(state)
        self.logger.error("Full backup failed: %s", exc)
        if settings.get("notify_on_failure", True):
            self._notify_failure(str(exc))

    def _notify_failure(self, message: str) -> None:
        try:
            from src.settings import load_settings as load_app_settings

            topic = str(load_app_settings().get("reminder_ntfy_topic") or "Reminders")
            base = os.getenv("ODYSSEUS_NTFY_INTERNAL_URL", "http://ntfy:80").rstrip("/")
            url = f"{base}/{urllib.parse.quote(topic, safe='')}"
            payload = f"Odysseus backup failed: {message}".encode("utf-8")
            request = urllib.request.Request(
                url,
                data=payload,
                headers={"Title": "Odysseus backup failed"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=10).close()
        except Exception as exc:
            self.logger.warning("Backup failure notification could not be sent: %s", exc)

    def _should_skip_dir(
        self, name: str, settings: Dict[str, Any], rel_root: str = "."
    ) -> bool:
        # __pycache__ is never user data and can appear anywhere.
        if name == "__pycache__":
            return True
        # Cache/model/derived directories are only excluded at the data root;
        # a user's own nested folder named "cache" must be preserved.
        if rel_root != ".":
            return False
        if name in _EXCLUDED_DIRS:
            return True
        if name == "mail-attachments" and not settings.get("include_attachments", True):
            return True
        if name == "deep_research" and not settings.get("include_research", True):
            return True
        return False

    def _estimate_source_size(self, settings: Dict[str, Any]) -> int:
        total = 0
        try:
            for root, dirs, files in os.walk(self.data_dir, topdown=True, followlinks=False):
                rel_root = os.path.relpath(root, self.data_dir)
                dirs[:] = [
                    d
                    for d in dirs
                    if not self._should_skip_dir(d, settings, rel_root)
                    and not os.path.islink(os.path.join(root, d))
                ]
                for filename in files:
                    path = os.path.join(root, filename)
                    if os.path.islink(path) or filename.endswith(("-journal", "-wal", "-shm")):
                        continue
                    try:
                        total += os.path.getsize(path)
                    except OSError:
                        pass
        except OSError:
            return 0
        return total

    def _copy_data(self, destination: str, settings: Dict[str, Any]) -> Tuple[int, int]:
        copied = 0
        total_bytes = 0
        for root, dirs, files in os.walk(self.data_dir, topdown=True, followlinks=False):
            rel_root = os.path.relpath(root, self.data_dir)
            dirs[:] = [
                d
                for d in dirs
                if not self._should_skip_dir(d, settings, rel_root)
                and not os.path.islink(os.path.join(root, d))
            ]
            for filename in files:
                if filename.endswith(("-journal", "-wal", "-shm")):
                    continue
                source = os.path.join(root, filename)
                if os.path.islink(source) or not os.path.isfile(source):
                    continue
                rel = filename if rel_root == "." else os.path.join(rel_root, filename)
                target = os.path.join(destination, rel)
                os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                if filename.endswith(".db"):
                    self._sqlite_snapshot(source, target)
                else:
                    shutil.copy2(source, target)
                try:
                    total_bytes += os.path.getsize(target)
                except OSError:
                    pass
                copied += 1
        return copied, total_bytes

    def _sqlite_snapshot(self, source: str, target: str) -> None:
        try:
            with open(source, "rb") as handle:
                header = handle.read(16)
        except OSError:
            header = b""
        if not header.startswith(b"SQLite format 3\x00"):
            shutil.copy2(source, target)
            return

        source_conn = None
        target_conn = None
        try:
            source_conn = sqlite3.connect(source, timeout=30)
            source_conn.execute("PRAGMA busy_timeout=30000")
            target_conn = sqlite3.connect(target)
            source_conn.backup(target_conn)
            check = target_conn.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise BackupError(
                    f"SQLite integrity check failed for {os.path.basename(source)}"
                )
        except sqlite3.DatabaseError as exc:
            raise BackupError(
                f"Could not snapshot {os.path.basename(source)}: {exc}"
            ) from exc
        finally:
            if target_conn is not None:
                target_conn.close()
            if source_conn is not None:
                source_conn.close()
        safe_chmod(target, 0o600)

    def _manifest_counts(self, stage_data: str) -> Dict[str, Any]:
        counts: Dict[str, Any] = {}
        db_path = os.path.join(stage_data, "app.db")
        if os.path.isfile(db_path):
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    for table in _COUNT_TABLES:
                        try:
                            counts[table] = conn.execute(
                                f'SELECT COUNT(*) FROM "{table}"'
                            ).fetchone()[0]
                        except sqlite3.Error:
                            continue
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
        auth_path = os.path.join(stage_data, "auth.json")
        if os.path.isfile(auth_path):
            try:
                with open(auth_path, "r", encoding="utf-8") as handle:
                    auth = json.load(handle)
                users = auth.get("users") if isinstance(auth, dict) else None
                if isinstance(users, dict):
                    counts["auth_users"] = len(users)
            except (OSError, json.JSONDecodeError):
                pass
        memory_path = os.path.join(stage_data, "memory.json")
        if os.path.isfile(memory_path):
            try:
                with open(memory_path, "r", encoding="utf-8") as handle:
                    memories = json.load(handle)
                if isinstance(memories, list):
                    counts["memory_entries"] = len(memories)
            except (OSError, json.JSONDecodeError):
                pass
        return counts

    def _read_sidecar(self, archive_path: str) -> Dict[str, Any]:
        sidecar = archive_path + ".manifest.json"
        try:
            with open(sidecar, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _read_sha(self, archive_path: str) -> str:
        sha_path = archive_path + ".sha256"
        try:
            with open(sha_path, "r", encoding="utf-8") as handle:
                first = handle.readline().strip().split()
            if first and re.fullmatch(r"[0-9a-fA-F]{64}", first[0]):
                return first[0].lower()
        except OSError:
            pass
        return ""

    def _prune(self, directory: str, retention: int, keep: str) -> int:
        retention = max(1, int(retention))
        candidates = []
        for name in os.listdir(directory):
            if not _ARCHIVE_RE.match(name):
                continue
            if not name.startswith("odysseus-full-auto-"):
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                candidates.append((os.path.getmtime(path), name, path))
            except OSError:
                continue
        candidates.sort(reverse=True)
        removed = 0
        for _mtime, name, path in candidates[retention:]:
            if name == keep:
                continue
            for candidate in (path, path + ".sha256", path + ".manifest.json"):
                try:
                    if os.path.isfile(candidate):
                        os.unlink(candidate)
                except OSError:
                    pass
            removed += 1

        # Clean up interrupted staging/archives older than a day.
        cutoff = time.time() - 86400
        for name in os.listdir(directory):
            if not name.startswith(".odysseus-backup-"):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) >= cutoff:
                    continue
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
            except OSError:
                pass
        return removed
