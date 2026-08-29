"""Restore a checked SQLite state backup while the service is stopped."""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from src.state_store import CURRENT_SCHEMA_VERSION, REQUIRED_STATE_TABLES, StateStore

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on non-POSIX deployments
    fcntl = None  # type: ignore[assignment]


# A V1 backup is migratable: its only allowed missing V2 object is the rule
# fingerprint table. The temporary restored copy is then opened by StateStore,
# which performs the complete migration and schema validation before install.
MIGRATABLE_PREVIOUS_SCHEMA_TABLES = (
    REQUIRED_STATE_TABLES - {"rule_config_fingerprints"}
)


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _assert_regular_or_missing(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"unable to inspect {label}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")


def _assert_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"unable to inspect {label}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be a directory")


def _displace_destination_sidecars(
    destination: Path,
) -> list[tuple[Path, Path]]:
    displaced: list[tuple[Path, Path]] = []
    try:
        for suffix in ("-wal", "-shm"):
            sidecar = destination.with_name(destination.name + suffix)
            _assert_regular_or_missing(sidecar, f"destination {suffix} sidecar")
            if not sidecar.exists():
                continue
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=f"{suffix}.old",
                dir=str(destination.parent),
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            temporary.unlink()
            os.replace(sidecar, temporary)
            displaced.append((sidecar, temporary))
    except Exception:
        _restore_destination_sidecars(displaced)
        raise
    return displaced


def _restore_destination_sidecars(
    displaced: list[tuple[Path, Path]],
) -> None:
    for original, temporary in reversed(displaced):
        if not temporary.exists() or original.exists():
            continue
        os.replace(temporary, original)


def _discard_displaced_sidecars(
    displaced: list[tuple[Path, Path]],
) -> None:
    for _, temporary in displaced:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # The new database is already installed; a cleanup failure must
            # not turn a successful restore into a false failure.
            pass


def _quick_check(path: Path) -> None:
    _assert_regular_or_missing(path, "SQLite database")
    for suffix in ("-wal", "-shm"):
        _assert_regular_or_missing(
            path.with_name(path.name + suffix),
            f"SQLite database {suffix} sidecar",
        )
    with closing(sqlite3.connect(str(path), timeout=1)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("source quick_check failed")
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise ValueError("source foreign key check failed")
        schema_row = connection.execute("PRAGMA user_version").fetchone()
        schema_version = int(schema_row[0]) if schema_row else 0
        if schema_version > CURRENT_SCHEMA_VERSION:
            raise ValueError(
                "source schema version is newer than this service"
            )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required_tables = (
            REQUIRED_STATE_TABLES
            if schema_version == CURRENT_SCHEMA_VERSION
            else MIGRATABLE_PREVIOUS_SCHEMA_TABLES
        )
        missing = sorted(required_tables - tables)
        if missing:
            raise ValueError(
                "source is missing required risk state tables: "
                + ", ".join(missing)
            )


def _server_lock_is_active(lock_path: Path) -> bool:
    """Check a POSIX advisory lock without trusting PID-file staleness."""

    _assert_regular_or_missing(lock_path, "destination server lock")
    if not lock_path.exists():
        return False
    if fcntl is None:
        # The fallback runtime has only the PID-file protocol. Treat any lock
        # file as active so restore remains fail-closed.
        return True
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return True
            raise
        return False
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _assert_destination_available(path: Path) -> None:
    _assert_regular_or_missing(path, "destination")
    server_lock = path.with_name(path.name + ".server.lock")
    if _server_lock_is_active(server_lock):
        raise RuntimeError(
            "destination has an active server lock; stop the risk service first"
        )
    for suffix in ("-wal", "-shm"):
        _assert_regular_or_missing(
            path.with_name(path.name + suffix),
            f"destination {suffix} sidecar",
        )
    if not path.exists():
        return
    try:
        with closing(sqlite3.connect(str(path), timeout=1)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "destination database is busy; stop the risk service first"
        ) from exc


def _record_restore_failure(path: Path, detail: str) -> None:
    """Record a failure only in an existing, unlocked risk database."""
    lock_path = path.with_name(path.name + ".server.lock")
    if (
        path.is_symlink()
        or lock_path.is_symlink()
        or not path.is_file()
    ):
        return
    try:
        if _server_lock_is_active(lock_path):
            return
    except (OSError, ValueError):
        return
    try:
        with closing(
            sqlite3.connect(str(path), timeout=0.2)
        ) as connection:
            table = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'backup_runs'
                """
            ).fetchone()
            if table is None:
                return
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO backup_runs (
                    operation, success, created_at, detail
                ) VALUES ('restore', 0, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    detail[:500],
                ),
            )
            connection.commit()
    except Exception:
        # Failure reporting must never touch or block the destination state.
        return


def restore(source: Path, destination: Path) -> Path:
    source = _absolute_path(source)
    destination = _absolute_path(destination)
    if source == destination:
        raise ValueError("source and destination must be different")
    StateStore._assert_no_symlink_components(source, "source")
    StateStore._assert_no_symlink_components(destination, "destination")
    StateStore._assert_no_symlink_components(
        source.parent,
        "source directory",
    )
    StateStore._assert_no_symlink_components(
        destination.parent,
        "destination directory",
    )
    _assert_directory(source.parent, "source directory")
    _assert_regular_or_missing(source, "source")
    if not source.is_file():
        raise FileNotFoundError(source)
    _quick_check(source)
    _assert_destination_available(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    StateStore._assert_no_symlink_components(destination, "destination")
    _assert_directory(destination.parent, "destination directory")
    temporary: Path | None = None
    displaced_sidecars: list[tuple[Path, Path]] = []
    replaced = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".restore",
            dir=str(destination.parent),
        )
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(temporary, 0o600)
        finally:
            os.close(descriptor)
        with closing(
            sqlite3.connect(str(source), timeout=1)
        ) as source_connection:
            target_connection = sqlite3.connect(str(temporary))
            try:
                source_connection.backup(target_connection)
                target_connection.commit()
            finally:
                target_connection.close()
        os.chmod(temporary, 0o600)
        _quick_check(temporary)
        migrated_store = StateStore(temporary)
        try:
            if not migrated_store.database_healthy():
                raise ValueError("restored state database health check failed")
            migrated_store.record_backup_event(
                operation="restore",
                success=True,
                detail=str(source),
            )
        finally:
            migrated_store.close()
        StateStore._checkpoint_file(temporary)
        _quick_check(temporary)
        StateStore._fsync_file(temporary)
        displaced_sidecars = _displace_destination_sidecars(destination)
        os.replace(temporary, destination)
        replaced = True
        _discard_displaced_sidecars(displaced_sidecars)
        StateStore._fsync_directory(destination.parent)
        return destination.resolve()
    except Exception as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if not replaced:
            _restore_destination_sidecars(displaced_sidecars)
        else:
            _discard_displaced_sidecars(displaced_sidecars)
        _record_restore_failure(destination, str(exc))
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument(
        "--state",
        default=os.environ.get("RISK_STATE_PATH", "runtime/risk-state.db"),
    )
    args = parser.parse_args()
    destination = restore(Path(args.source), Path(args.state))
    print(
        json.dumps(
            {
                "ok": True,
                "operation": "restore",
                "source": str(Path(args.source).resolve()),
                "state": str(destination),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
