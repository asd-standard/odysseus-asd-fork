#!/usr/bin/env python3
"""Export/import an Odysseus `data/` directory for a Docker deployment.

The Docker image keeps all state under ``/app/data`` (a bind mount), so a
migration is "copy ``data/`` to the target host", with two extra rules:

1. SQLite files are snapshotted with SQLite's online backup API instead of a
   raw copy, so a running app cannot produce a torn database.
2. ``data/uploads/uploads.json`` stores absolute paths. Those paths are
   rewritten to the container mount (``/app/data/uploads/...``) so attachment
   lookups still pass the app's "inside the upload dir" check.

Usage (on the old machine):

    python deploy/migrate_data.py export --out /tmp/odysseus-data.tar.gz

Usage (on the new machine, after copying the bundle and its .sha256):

    python deploy/migrate_data.py import /tmp/odysseus-data.tar.gz

The bundle always contains entries under ``data/``, so it can also be
extracted manually into the deployment directory. The import command is
preferred because it verifies checksums, paths, and upload files, and it
refuses to overwrite a non-empty data directory unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_NEW_DATA_ROOT = "/app/data"
BUNDLE_PREFIX = "data"

# Re-derivable or machine-specific state. User content (uploads, documents,
# skills, research output, the encryption key, auth) is never in this list.
EXCLUDED_TOP_DIRS = {
    "cache",                # fetched web content cache
    "local",                # container/host-specific pip + browser caches
    "huggingface",          # model download cache
    "emoji_cache",
    "tts_cache",
    "email_urgency_cache",
    "hwfit",                # hardware-fit cache for this machine
    "logs",                 # contains past queries; start the new host fresh
}
EXCLUDED_TOP_FILES = {"sessions.json"}  # old origins/cookies; users re-login
EXCLUDED_DIR_NAMES = {"__pycache__"}
EXCLUDED_FILE_NAMES = {".DS_Store", "Thumbs.db"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", "-wal", "-shm", "-journal")

REQUIRED_FILES = ("app.db", "auth.json", ".app_key")


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_excluded(rel: PurePosixPath) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in EXCLUDED_TOP_DIRS:
        return True
    if len(parts) == 1 and parts[0] in EXCLUDED_TOP_FILES:
        return True
    if any(part in EXCLUDED_DIR_NAMES for part in parts):
        return True
    name = parts[-1]
    if name.startswith(".uploads-"):  # in-flight atomic writes
        return True
    if name in EXCLUDED_FILE_NAMES or name.endswith(EXCLUDED_SUFFIXES):
        return True
    return False


def _iter_data_files(data_dir: Path):
    """Yield relative POSIX paths for every file the bundle should carry."""
    for root, dirs, files in os.walk(data_dir):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIR_NAMES)
        for name in sorted(files):
            source = Path(root) / name
            rel = PurePosixPath(source.relative_to(data_dir).as_posix())
            if _is_excluded(rel):
                continue
            if source.is_symlink():
                # HuggingFace/FastEmbed caches are symlink farms. Preserve the
                # links, but only when they stay inside data/ (no escaping).
                resolved = source.resolve()
                try:
                    resolved.relative_to(data_dir.resolve())
                except ValueError:
                    print(f"  [warn] skipping out-of-tree symlink: {rel}", file=sys.stderr)
                    continue
                if not resolved.is_file():
                    print(f"  [warn] skipping dangling symlink: {rel}", file=sys.stderr)
                    continue
                yield rel, source
                continue
            if not source.is_file():
                continue
            yield rel, source


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    """Copy a SQLite database with its online backup API (safe while in use)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(destination))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def _rewrite_upload_paths(payload: dict, new_root: str) -> tuple[dict, int, list[str]]:
    """Rewrite every ``path`` in an uploads.json payload to the Docker root.

    Paths start with ``<old data dir>/uploads/`` (or already with the new
    root). Everything from the ``/uploads/`` marker on is preserved, which
    makes the operation independent of where the source checkout lived.
    """
    new_uploads = new_root.rstrip("/") + "/uploads"
    rewritten = 0
    problems: list[str] = []
    if not isinstance(payload, dict):
        return payload, rewritten, ["uploads index is not a JSON object"]

    for key, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        marker = "/uploads/"
        index = path.find(marker)
        if index == -1:
            problems.append(f"{key}: path is not under an uploads directory: {path}")
            continue
        tail = path[index + len(marker):]
        entry["path"] = f"{new_uploads}/{tail}"
        rewritten += 1
    return payload, rewritten, problems


def _read_upload_paths(uploads_file: Path) -> dict:
    try:
        with uploads_file.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _source_upload_problems(payload: dict, source_uploads_dir: Path) -> list[str]:
    """Check indexed uploads still exist before they leave the source host."""
    problems: list[str] = []
    for key, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        marker = "/uploads/"
        index = path.find(marker)
        if index == -1:
            problems.append(f"{key}: unrecognised upload path {path}")
            continue
        tail = path[index + len(marker):]
        if not (source_uploads_dir / tail).is_file():
            problems.append(f"{key}: file missing on source: uploads/{tail}")
    return problems


def _count_users(auth_file: Path) -> list[str]:
    try:
        with auth_file.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        users = payload.get("users")
        if isinstance(users, dict):
            return sorted(users)
    except Exception:
        pass
    return []


def _db_owner_summary(db_file: Path) -> dict:
    """Per-user row counts for the tables that hold user content."""
    tables = (
        "sessions",
        "documents",
        "folders",
        "notes",
        "calendars",
        "scheduled_tasks",
        "crew_members",
        "gallery_images",
        "memories",
        "model_endpoints",
    )
    summary: dict[str, dict[str, int]] = {}
    try:
        conn = sqlite3.connect(str(db_file))
        try:
            for table in tables:
                try:
                    rows = conn.execute(
                        f'SELECT COALESCE(owner, "(shared)"), COUNT(*) '
                        f'FROM "{table}" GROUP BY owner ORDER BY owner'
                    ).fetchall()
                except sqlite3.Error:
                    continue
                summary[table] = {str(owner): int(count) for owner, count in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return summary


def _add_file(tar: tarfile.TarFile, source: Path, rel: PurePosixPath) -> int:
    info = tar.gettarinfo(str(source), arcname=f"{BUNDLE_PREFIX}/{rel.as_posix()}")
    if info.isfile():
        with source.open("rb") as fh:
            tar.addfile(info, fh)
    else:
        tar.addfile(info)
    return int(info.size)


def cmd_export(args) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    if not data_dir.is_dir():
        print(f"error: data directory not found: {data_dir}", file=sys.stderr)
        return 1

    out = Path(args.out).expanduser().resolve() if args.out else (
        REPO_ROOT / f"odysseus-data-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    )
    try:
        out.relative_to(data_dir)
    except ValueError:
        pass
    else:
        print("error: bundle output must be outside the data directory", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)

    new_root = args.new_data_root.rstrip("/")
    users = _count_users(data_dir / "auth.json")

    print(f"Exporting {data_dir}")
    print(f"  users:  {', '.join(users) if users else '(none found)'}")
    print(f"  target: {out}")

    files = list(_iter_data_files(data_dir))
    missing_source_uploads: list[str] = []
    rewritten = 0
    db_files: list[str] = []
    db_summary: dict = {}
    total_bytes = 0
    file_count = 0

    with tempfile.TemporaryDirectory(prefix="odysseus-export-") as tmp_str:
        tmp = Path(tmp_str)
        with tarfile.open(out, "w:gz", compresslevel=6) as tar:
            for rel, source in files:
                target = source
                if rel.suffix == ".db":
                    snapshot = tmp / "dbs" / rel
                    _snapshot_sqlite(source, snapshot)
                    try:
                        os.chmod(snapshot, source.stat().st_mode & 0o777)
                    except OSError:
                        pass
                    target = snapshot
                    db_files.append(rel.as_posix())
                    if rel.as_posix() == "app.db":
                        db_summary = _db_owner_summary(snapshot)
                elif rel.as_posix() in ("uploads/uploads.json", "uploads/uploads.json.bak"):
                    payload = _read_upload_paths(source)
                    is_live_index = rel.as_posix() == "uploads/uploads.json"
                    if is_live_index:
                        missing_source_uploads = _source_upload_problems(
                            payload, data_dir / "uploads"
                        )
                    payload, changed, problems = _rewrite_upload_paths(payload, new_root)
                    if is_live_index:
                        rewritten += changed
                    for problem in problems:
                        print(f"  [warn] {rel}: {problem}", file=sys.stderr)
                    target = tmp / "rewritten" / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("w", encoding="utf-8") as fh:
                        json.dump(payload, fh, indent=2, ensure_ascii=False)
                    try:
                        os.chmod(target, source.stat().st_mode & 0o777)
                    except OSError:
                        pass
                total_bytes += _add_file(tar, target, rel)
                file_count += 1

    checksum = _sha256(out)
    checksum_file = out.with_name(out.name + ".sha256")
    checksum_file.write_text(f"{checksum}  {out.name}\n", encoding="utf-8")

    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "source_data_dir": str(data_dir),
        "target_data_root": new_root,
        "users": users,
        "file_count": file_count,
        "uncompressed_bytes": total_bytes,
        "bundle_bytes": out.stat().st_size,
        "bundle_sha256": checksum,
        "sqlite_snapshots": db_files,
        "uploads_rewritten": rewritten,
        "missing_source_uploads": missing_source_uploads,
        "owner_summary": db_summary,
    }
    manifest_file = out.with_name(out.name + ".manifest.json")
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"\nCreated {out} ({_human_bytes(out.stat().st_size)})")
    print(f"  files:            {file_count}")
    print(f"  uncompressed:     {_human_bytes(total_bytes)}")
    print(f"  uploads indexed:  {rewritten} path(s) rewritten to {new_root}/uploads")
    print(f"  sqlite snapshots: {', '.join(db_files) or '(none)'}")
    print(f"  sha256:           {checksum}")
    print(f"  checksum file:    {checksum_file}")
    print(f"  manifest:         {manifest_file}")
    if missing_source_uploads:
        print(
            f"\n  [warn] {len(missing_source_uploads)} indexed upload(s) are already "
            "missing on the source host:",
            file=sys.stderr,
        )
        for problem in missing_source_uploads[:10]:
            print(f"    - {problem}", file=sys.stderr)
        if len(missing_source_uploads) > 10:
            print(f"    ... and {len(missing_source_uploads) - 10} more", file=sys.stderr)

    if not (data_dir / ".app_key").is_file():
        print("\n  [warn] data/.app_key is missing: encrypted API keys, email "
              "passwords, and CalDAV credentials will not decrypt on the target.",
              file=sys.stderr)
    print("\nNext: stop the app on this host (if still running), copy the bundle "
          "plus its .sha256 file to the target, then run the import command there.")
    return 0


def _validate_members(members: list[tarfile.TarInfo]) -> None:
    for member in members:
        rel = PurePosixPath(member.name)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"refusing absolute/parent path in bundle: {member.name!r}")
        if not rel.parts or rel.parts[0] != BUNDLE_PREFIX:
            raise ValueError(f"refusing entry outside {BUNDLE_PREFIX}/: {member.name!r}")
        if member.issym():
            link = PurePosixPath(member.linkname)
            if link.is_absolute():
                raise ValueError(f"refusing absolute symlink target: {member.name!r}")
            resolved = posixpath.normpath(
                str(PurePosixPath(member.name).parent / member.linkname)
            )
            if resolved != BUNDLE_PREFIX and not resolved.startswith(BUNDLE_PREFIX + "/"):
                raise ValueError(f"refusing symlink escaping {BUNDLE_PREFIX}/: {member.name!r}")
            continue
        if member.islnk():
            raise ValueError(f"refusing hardlink entry in bundle: {member.name!r}")
        if not (member.isdir() or member.isfile()):
            raise ValueError(f"refusing special file in bundle: {member.name!r}")


def _safe_extract(bundle: Path, destination: Path) -> None:
    with tarfile.open(bundle, "r:gz") as tar:
        members = tar.getmembers()
        _validate_members(members)
        symlinks: list[tarfile.TarInfo] = []
        # Create regular files/dirs first, links last. Every validated link
        # stays under data/, so it cannot redirect a later write outside.
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                symlinks.append(member)
                continue
            source = tar.extractfile(member)
            if source is None:
                raise ValueError(f"could not read {member.name!r} from bundle")
            with source, target.open("wb") as fh:
                shutil.copyfileobj(source, fh)
            try:
                os.chmod(target, member.mode & 0o777)
            except OSError:
                pass
        for member in symlinks:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            os.symlink(member.linkname, target)


def _rewrite_import_uploads(data_dir: Path, new_root: str) -> tuple[int, list[str], list[str]]:
    """Normalise upload paths in the imported tree and validate the files."""
    rewritten = 0
    problems: list[str] = []
    missing: list[str] = []
    new_uploads = (new_root.rstrip("/") + "/uploads").rstrip("/")
    for name in ("uploads.json", "uploads.json.bak"):
        uploads_file = data_dir / "uploads" / name
        if not uploads_file.is_file():
            continue
        is_live_index = name == "uploads.json"
        payload = _read_upload_paths(uploads_file)
        payload, changed, rewrite_problems = _rewrite_upload_paths(payload, new_root)
        if is_live_index:
            rewritten += changed
            problems.extend(f"{name}: {p}" for p in rewrite_problems)
        else:
            # The .bak index is a rollback aid, not authoritative. Rewrite it,
            # but stale entries there must not block an otherwise good import.
            for problem in rewrite_problems:
                print(f"  [warn] {name}: {problem}", file=sys.stderr)

        for key, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not isinstance(path, str) or not path:
                continue
            if not path.startswith(new_uploads + "/"):
                message = f"{name}: {key}: path escaped {new_uploads}: {path}"
                if is_live_index:
                    problems.append(message)
                else:
                    print(f"  [warn] {message}", file=sys.stderr)
                continue
            rel = path[len(new_uploads) + 1:]
            if not (data_dir / "uploads" / rel).is_file() and is_live_index:
                missing.append(f"{key}: uploads/{rel}")
        with uploads_file.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    return rewritten, problems, missing


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            try:
                os.chown(Path(current) / name, uid, gid)
            except OSError as exc:
                print(f"  [warn] could not chown {Path(current) / name}: {exc}", file=sys.stderr)
    try:
        os.chown(root, uid, gid)
    except OSError:
        pass


def _resolve_id(explicit: str | None, env_name: str) -> int | None:
    raw = explicit or os.getenv(env_name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        print(f"error: {env_name} must be numeric, got {raw!r}", file=sys.stderr)
        raise SystemExit(2)


def cmd_import(args) -> int:
    bundle = Path(args.bundle).expanduser().resolve()
    if not bundle.is_file():
        print(f"error: bundle not found: {bundle}", file=sys.stderr)
        return 1

    checksum_file = bundle.with_name(bundle.name + ".sha256")
    if checksum_file.is_file():
        expected = checksum_file.read_text(encoding="utf-8").split()[0].strip()
        actual = _sha256(bundle)
        if expected != actual:
            print("error: bundle checksum does not match its .sha256 file", file=sys.stderr)
            print(f"  expected: {expected}", file=sys.stderr)
            print(f"  actual:   {actual}", file=sys.stderr)
            return 1
        print(f"Checksum OK: {actual}")
    else:
        print("  [warn] no .sha256 file next to the bundle; skipping checksum verification",
              file=sys.stderr)

    data_dir = Path(args.data_dir).expanduser().resolve()
    new_root = args.new_data_root.rstrip("/")
    data_dir.parent.mkdir(parents=True, exist_ok=True)

    stash: Path | None = None
    if data_dir.exists() and any(data_dir.iterdir()):
        if not args.force:
            print(f"error: {data_dir} already exists and is not empty", file=sys.stderr)
            print("       move it aside, or re-run with --force to stash it automatically",
                  file=sys.stderr)
            return 1
        stash = data_dir.with_name(
            f"{data_dir.name}.before-import-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        os.rename(data_dir, stash)
        print(f"Existing data stashed at {stash}")
    elif data_dir.exists():
        data_dir.rmdir()

    staging_dir = Path(tempfile.mkdtemp(prefix="odysseus-import-", dir=str(data_dir.parent)))
    try:
        print(f"Extracting {bundle} ...")
        _safe_extract(bundle, staging_dir)
        extracted = staging_dir / BUNDLE_PREFIX
        if not extracted.is_dir():
            raise ValueError("bundle does not contain a data/ directory")

        missing_required = [name for name in REQUIRED_FILES if not (extracted / name).is_file()]
        if missing_required:
            raise ValueError(
                "bundle is incomplete, missing: " + ", ".join(missing_required)
            )

        rewritten, problems, missing_uploads = _rewrite_import_uploads(extracted, new_root)
        if problems:
            for problem in problems[:20]:
                print(f"  [error] {problem}", file=sys.stderr)
            raise ValueError(f"{len(problems)} upload path problem(s); refusing to import")
        if missing_uploads:
            for problem in missing_uploads[:20]:
                print(f"  [error] {problem}", file=sys.stderr)
            if not args.allow_missing_uploads:
                raise ValueError(
                    f"{len(missing_uploads)} indexed upload file(s) are missing; "
                    "re-run with --allow-missing-uploads to keep the index anyway"
                )
            print("  [warn] continuing with --allow-missing-uploads", file=sys.stderr)

        if data_dir.exists():
            raise ValueError(f"destination appeared unexpectedly: {data_dir}")
        os.rename(extracted, data_dir)
    except Exception as exc:
        print(f"error: import failed: {exc}", file=sys.stderr)
        if stash is not None and stash.exists() and not data_dir.exists():
            os.rename(stash, data_dir)
            print(f"Restored previous data from {stash}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    key_file = data_dir / ".app_key"
    # Secrets and databases should not be group/world readable on the host.
    for name in (".app_key", "auth.json", "app.db", "scheduled_emails.db"):
        secret_file = data_dir / name
        if secret_file.is_file():
            try:
                os.chmod(secret_file, 0o600)
            except OSError:
                pass

    uid = _resolve_id(args.puid, "PUID")
    gid = _resolve_id(args.pgid, "PGID")
    if os.geteuid() == 0 and uid is not None and gid is not None:
        print(f"Chowning {data_dir} to {uid}:{gid} ...")
        _chown_tree(data_dir, uid, gid)

    users = _count_users(data_dir / "auth.json")
    upload_count = 0
    uploads_file = data_dir / "uploads" / "uploads.json"
    if uploads_file.is_file():
        upload_count = len(_read_upload_paths(uploads_file))

    print(f"\nImported into {data_dir}")
    print(f"  users:            {', '.join(users) if users else '(none)'}")
    print(f"  uploads indexed:  {upload_count} ({rewritten} path(s) normalised to {new_root}/uploads)")
    print(f"  encryption key:   {'present' if key_file.is_file() else 'MISSING'}")
    if os.geteuid() == 0 and (uid is None or gid is None):
        print("  [warn] PUID/PGID not set; the container entrypoint will chown on first start")
    print("\nNext: configure .env (APP_BIND, PUID, PGID, ALLOWED_ORIGINS), "
          "then run `docker compose up -d`.")
    return 0


def cmd_verify(args) -> int:
    bundle = Path(args.bundle).expanduser().resolve()
    if not bundle.is_file():
        print(f"error: bundle not found: {bundle}", file=sys.stderr)
        return 1
    checksum_file = bundle.with_name(bundle.name + ".sha256")
    checksum = _sha256(bundle)
    if checksum_file.is_file():
        expected = checksum_file.read_text(encoding="utf-8").split()[0].strip()
        if expected != checksum:
            print(f"error: checksum mismatch: expected {expected}, got {checksum}", file=sys.stderr)
            return 1
    with tarfile.open(bundle, "r:gz") as tar:
        members = tar.getmembers()
        _validate_members(members)
        names = {member.name for member in members}
    missing = [
        f"{BUNDLE_PREFIX}/{name}"
        for name in REQUIRED_FILES
        if f"{BUNDLE_PREFIX}/{name}" not in names
    ]
    if missing:
        print(f"error: bundle is missing required file(s): {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"Bundle OK: {bundle}")
    print(f"  members: {len(members)}")
    print(f"  sha256:  {checksum}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_data.py",
        description="Export/import Odysseus data for a Docker deployment.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="create a migration bundle from a data/ directory")
    export.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help=f"source data directory (default: {DEFAULT_DATA_DIR})")
    export.add_argument("--out", default=None,
                        help="output .tar.gz (default: ./odysseus-data-<timestamp>.tar.gz)")
    export.add_argument("--new-data-root", default=DEFAULT_NEW_DATA_ROOT,
                        help=f"container data mount used in rewritten upload paths "
                             f"(default: {DEFAULT_NEW_DATA_ROOT})")
    export.set_defaults(func=cmd_export)

    imp = sub.add_parser("import", help="extract and validate a migration bundle")
    imp.add_argument("bundle", help="path to odysseus-data-*.tar.gz")
    imp.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                     help=f"destination data directory (default: {DEFAULT_DATA_DIR})")
    imp.add_argument("--new-data-root", default=DEFAULT_NEW_DATA_ROOT,
                     help=f"container data mount expected in upload paths "
                          f"(default: {DEFAULT_NEW_DATA_ROOT})")
    imp.add_argument("--force", action="store_true",
                     help="stash a non-empty destination data/ aside and continue")
    imp.add_argument("--allow-missing-uploads", action="store_true",
                     help="keep going when uploads.json references missing files")
    imp.add_argument("--puid", default=None, help="chown imported data to this UID (root only)")
    imp.add_argument("--pgid", default=None, help="chown imported data to this GID (root only)")
    imp.set_defaults(func=cmd_import)

    verify = sub.add_parser("verify", help="verify a bundle without extracting it")
    verify.add_argument("bundle", help="path to odysseus-data-*.tar.gz")
    verify.set_defaults(func=cmd_verify)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
