"""In-process scheduler for automatic full backups."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from .service import BackupBusy, BackupError, BackupService


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class BackupScheduler:
    """Check once a minute and run a backup when the configured interval is due."""

    def __init__(self, service: BackupService, logger: Optional[logging.Logger] = None):
        self.service = service
        self.logger = logger or logging.getLogger("services.backup.scheduler")
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        # A process killed mid-backup can leave `running: true` behind. The
        # in-process lock is necessarily free after a restart, so clear the
        # stale flag instead of blocking every future scheduled run.
        try:
            state = config.load_state()
            if state.get("running"):
                state["running"] = False
                if not state.get("last_error"):
                    state["last_error"] = (
                        "Previous backup was interrupted by an application restart"
                    )
                config.save_state(state)
                self.logger.warning("Cleared stale backup running flag from a previous process")
        except Exception as exc:
            self.logger.warning("Could not reset interrupted backup state: %s", exc)

        gate = os.getenv("ODYSSEUS_INPROCESS_BACKUPS", "1").strip().lower()
        if gate in {"0", "false", "no", "off"}:
            self.logger.info(
                "In-process backup scheduler disabled (ODYSSEUS_INPROCESS_BACKUPS=0)"
            )
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="odysseus-backup-scheduler")
        self.logger.info("In-process backup scheduler started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    def _due(self) -> bool:
        settings = config.load_settings()
        if not settings.get("enabled"):
            return False
        state = config.load_state()
        if state.get("running"):
            return False
        last = _parse_time(state.get("last_success"))
        if last is None:
            return True
        interval = int(settings.get("interval_hours", config.DEFAULT_INTERVAL_HOURS))
        return datetime.now(timezone.utc) >= last + timedelta(hours=interval)

    async def _loop(self) -> None:
        # Let startup finish before doing disk I/O.
        await asyncio.sleep(30)
        while True:
            try:
                if self._due():
                    await asyncio.to_thread(self.service.create_backup, "scheduled")
            except BackupBusy:
                self.logger.debug("Backup scheduler skipped: a backup is already running")
            except BackupError as exc:
                self.logger.warning("Scheduled backup failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("Scheduled backup crashed: %s", exc)
            await asyncio.sleep(60)
