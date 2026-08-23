"""Restore a checked SQLite state backup while the service is stopped."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from src.state_store import StateStore


def _quick_check(path: Path) -> None:
    with closing(sqlite3.connect(str(path), timeout=1)) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("source quick_check failed")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "accounts" not in tables or "calendar_snapshots" not in tables:
            raise ValueError("source is not an FTMO risk state database")


def _assert_destination_available(path: Path) -> None:
    server_lock = path.with_name(path.name + ".server.lock")
    if server_lock.exists():
        raise RuntimeError(
            "destination has an active server lock; stop the risk service first"
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


def restore(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if source == destination:
        raise ValueError("source and destination must be different")
    if not source.is_file():
        raise FileNotFoundError(source)
    _quick_check(source)
    _assert_destination_available(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{secrets.token_hex(8)}.restore"
    )
    try:
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
        os.replace(temporary, destination)
        destination.with_name(destination.name + "-wal").unlink(
            missing_ok=True
        )
        destination.with_name(destination.name + "-shm").unlink(
            missing_ok=True
        )
        os.chmod(destination, 0o600)
        StateStore(destination).record_backup_event(
            operation="restore",
            success=True,
            detail=str(source),
        )
        return destination
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        try:
            StateStore(destination).record_backup_event(
                operation="restore",
                success=False,
                detail=str(exc),
            )
        except Exception:
            pass
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
