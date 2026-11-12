"""Focused tests for the admin full-instance backup service.

These tests use a private data directory and backup root; they do not touch the
real application database.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from services.backup import BackupService, config


def _make_data(root: Path) -> Path:
    data = root / "data"
    data.mkdir(parents=True)
    db = sqlite3.connect(data / "app.db")
    try:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, owner TEXT)")
        db.execute("INSERT INTO sessions VALUES ('s1', 'admin')")
        db.execute(
            "CREATE TABLE chat_messages (id TEXT PRIMARY KEY, session_id TEXT, content TEXT)"
        )
        db.execute("INSERT INTO chat_messages VALUES ('m1', 's1', 'hello')")
        db.commit()
    finally:
        db.close()
    (data / "auth.json").write_text(
        json.dumps({"users": {"admin": {"is_admin": True}}}), encoding="utf-8"
    )
    (data / "memory.json").write_text(
        json.dumps([{"id": "mem1", "text": "remember", "owner": "admin"}]),
        encoding="utf-8",
    )
    (data / "sessions.json").write_text("{}", encoding="utf-8")
    (data / ".app_key").write_text("test-key", encoding="utf-8")
    (data / "uploads" / "2026" / "10").mkdir(parents=True)
    (data / "uploads" / "2026" / "10" / "note.txt").write_text(
        "user file", encoding="utf-8"
    )
    (data / "fastembed_cache").mkdir()
    (data / "fastembed_cache" / "model.bin").write_bytes(b"x" * 1024)
    (data / "logs").mkdir()
    (data / "logs" / "app.log").write_text("log", encoding="utf-8")
    (data / "deep_research").mkdir()
    (data / "deep_research" / "run.json").write_text(
        json.dumps({"owner": "admin"}), encoding="utf-8"
    )
    (data / "mail-attachments").mkdir()
    (data / "mail-attachments" / "att.txt").write_text("attachment", encoding="utf-8")
    return data


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    data = _make_data(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setenv("ODYSSEUS_BACKUP_ROOTS", str(backups))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(data / "backup_settings.json"))
    monkeypatch.setattr(config, "STATE_FILE", str(data / "backup_state.json"))
    config.save_settings(
        {
            "enabled": True,
            "directory": str(backups),
            "interval_hours": 8,
            "retention": 2,
            "include_attachments": True,
            "include_research": True,
            "notify_on_failure": False,
        }
    )
    return data, backups


def test_full_backup_archive_contents(tmp_path, monkeypatch):
    data, backups = _configure(tmp_path, monkeypatch)
    service = BackupService(data_dir=str(data))

    info = service.create_backup("manual")
    assert info["name"].startswith("odysseus-full-manual-")
    archive = backups / info["name"]
    assert archive.is_file()
    assert archive.with_name(archive.name + ".sha256").is_file()
    assert archive.with_name(archive.name + ".manifest.json").is_file()

    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
        manifest = json.load(tar.extractfile("manifest.json"))

    assert "manifest.json" in names
    assert "data/app.db" in names
    assert "data/auth.json" in names
    assert "data/.app_key" in names
    assert "data/uploads/2026/10/note.txt" in names
    assert "data/deep_research/run.json" in names
    assert "data/mail-attachments/att.txt" in names
    assert not any("fastembed_cache" in name for name in names)
    assert not any(name.startswith("data/logs/") for name in names)
    assert manifest["counts"]["sessions"] == 1
    assert manifest["counts"]["chat_messages"] == 1
    assert manifest["counts"]["auth_users"] == 1
    assert manifest["format"] == "odysseus-full-backup"

    listed = service.list_backups()
    assert [item["name"] for item in listed] == [info["name"]]
    assert service.resolve_backup(info["name"]) == str(archive)

    service.delete_backup(info["name"])
    assert not archive.exists()
    assert not archive.with_name(archive.name + ".sha256").exists()


def test_automatic_retention_rotates_only_automatic_backups(tmp_path, monkeypatch):
    data, backups = _configure(tmp_path, monkeypatch)
    service = BackupService(data_dir=str(data))
    manual = service.create_backup("manual")
    automatic = []
    for _ in range(3):
        time.sleep(1.1)
        automatic.append(service.create_backup("scheduled")["name"])

    names = {item["name"] for item in service.list_backups()}
    assert manual["name"] in names
    assert len([name for name in names if name.startswith("odysseus-full-auto-")]) == 2
    assert automatic[-1] in names


def test_restore_script_verifies_backup(tmp_path, monkeypatch):
    data, backups = _configure(tmp_path, monkeypatch)
    service = BackupService(data_dir=str(data))
    info = service.create_backup("manual")
    archive = backups / info["name"]
    script = Path(__file__).resolve().parents[1] / "scripts" / "odysseus-restore"
    assert script.is_file(), script

    result = subprocess.run(
        [sys.executable, str(script), "--verify", str(archive)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "checksum OK" in result.stdout
    assert "SQLite integrity: ok" in result.stdout
    assert "sessions: 1" in result.stdout
