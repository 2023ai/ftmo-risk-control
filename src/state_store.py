"""Persistent account, settlement, idempotency, and frequency state."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from hmac import compare_digest
from typing import Any, Callable, Mapping
from uuid import uuid4

from .risk_engine import (
    AccountPhase,
    AccountSnapshot,
    AccountStyle,
    AccountType,
    FrequencyState,
    RiskEngine,
    RuleProfile,
    ZERO,
    ftmo_day_key,
)


ACCOUNT_CREDENTIAL_SCOPES = frozenset(
    {
        "account:sync",
        "account:settlement",
        "trade:evaluate",
        "trade:execution",
        "calendar:read",
        "qualification:read",
        "qualification:write",
    }
)

# The default is deliberately limited to the platform adapter's runtime work.
# Settlement and qualification imports must use separately issued credentials.
PLATFORM_CREDENTIAL_SCOPES = frozenset(
    {
        "account:sync",
        "trade:evaluate",
        "trade:execution",
        "calendar:read",
    }
)

CREDENTIAL_LAST_USED_WRITE_INTERVAL = timedelta(seconds=60)
CALENDAR_HASH_VERSION = 2
DATABASE_INTEGRITY_CHECK_INTERVAL_SECONDS = 60.0
CURRENT_SCHEMA_VERSION = 2
ACTIVE_RESERVATION_STATUSES = ("pending", "unknown", "committed")
UNRESOLVED_RESERVATION_STATUSES = ("pending", "unknown")
# A restore must contain every table that carries state which can affect a
# future risk decision.  In particular, reservations cannot be recreated as
# an empty table during startup migration without losing in-flight risk.
REQUIRED_STATE_TABLES = frozenset(
    {
        "accounts",
        "activity",
        "daily_settlements",
        "decisions",
        "executions",
        "risk_reservations",
        "calendar_snapshots",
        "account_credentials",
        "closed_trades",
        "qualification_history_status",
        "qualification_trading_days",
        "rule_config_fingerprints",
        "backup_runs",
    }
)
REQUIRED_STATE_COLUMNS = {
    "accounts": frozenset(
        {
            "account_id",
            "account_type",
            "phase",
            "style",
            "initial_capital",
            "ftmo_day",
            "day_start_balance",
            "highest_settled_balance",
            "balance",
            "equity",
            "current_open_risk",
            "reserved_open_risk",
            "open_positions_count",
            "pending_orders_count",
            "as_of",
            "updated_at",
            "data_uncertain",
            "day_locked",
            "breach_latched",
        }
    ),
    "activity": frozenset(
        {
            "id",
            "account_id",
            "kind",
            "symbol",
            "occurred_at",
            "request_id",
            "detail",
        }
    ),
    "daily_settlements": frozenset(
        {
            "account_id",
            "ftmo_day",
            "settled_balance",
            "settled_at",
            "source",
            "confirmed",
        }
    ),
    "decisions": frozenset(
        {
            "account_id",
            "request_id",
            "request_hash",
            "action",
            "symbol",
            "allowed",
            "reservation_risk",
            "response_json",
            "created_at",
        }
    ),
    "executions": frozenset(
        {
            "account_id",
            "request_id",
            "request_hash",
            "action",
            "symbol",
            "outcome",
            "response_json",
            "created_at",
        }
    ),
    "risk_reservations": frozenset(
        {
            "account_id",
            "request_id",
            "request_hash",
            "action",
            "symbol",
            "reserved_risk",
            "status",
            "created_at",
            "updated_at",
            "execution_at",
        }
    ),
    "calendar_snapshots": frozenset(
        {
            "calendar_type",
            "fetched_at",
            "coverage_start",
            "coverage_end",
            "hash_version",
            "content_hash",
            "payload_json",
            "rule_version",
            "created_at",
        }
    ),
    "account_credentials": frozenset(
        {
            "credential_id",
            "account_id",
            "secret_salt",
            "secret_hash",
            "not_before",
            "expires_at",
            "revoked_at",
            "last_used_at",
            "scopes_json",
            "created_at",
        }
    ),
    "closed_trades": frozenset(
        {
            "account_id",
            "trade_id",
            "phase",
            "cycle_id",
            "closed_at",
            "ftmo_day",
            "net_profit",
            "symbol",
            "source",
            "request_id",
            "created_at",
        }
    ),
    "qualification_history_status": frozenset(
        {
            "account_id",
            "phase",
            "cycle_id",
            "history_start_at",
            "complete_through",
            "source",
            "updated_at",
        }
    ),
    "qualification_trading_days": frozenset(
        {
            "account_id",
            "phase",
            "cycle_id",
            "ftmo_day",
            "first_opened_at",
            "source",
            "request_id",
            "created_at",
        }
    ),
    "rule_config_fingerprints": frozenset(
        {
            "rule_version",
            "config_fingerprint",
            "recorded_at",
        }
    ),
    "backup_runs": frozenset(
        {
            "id",
            "operation",
            "success",
            "created_at",
            "detail",
        }
    ),
}
# An approval is an authorization for one submission attempt, not a reusable
# trade ticket. Pending reservations that outlive the lease are promoted to
# unknown so a lost client response cannot silently free risk.
PENDING_RESERVATION_LEASE = timedelta(seconds=60)
MAX_CREDENTIAL_SECRET_LENGTH = 512


def _missing_required_schema_objects(
    connection: sqlite3.Connection,
) -> list[str]:
    tables = {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = sorted(REQUIRED_STATE_TABLES - tables)
    for table, required_columns in REQUIRED_STATE_COLUMNS.items():
        if table not in tables:
            continue
        columns = {
            str(row["name"])
            for row in connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        missing.extend(
            f"{table}.{column}"
            for column in sorted(required_columns - columns)
        )
    return missing


def _calendar_content_hash(
    *,
    calendar_type: str,
    fetched_at: datetime,
    coverage_start: datetime | None,
    coverage_end: datetime | None,
    payload: list[dict[str, Any]],
    rule_version: str,
) -> str:
    encoded = json.dumps(
        {
            "hash_version": CALENDAR_HASH_VERSION,
            "calendar_type": calendar_type,
            "fetched_at": fetched_at.isoformat(),
            "coverage_start": (
                coverage_start.isoformat()
                if coverage_start is not None
                else None
            ),
            "coverage_end": (
                coverage_end.isoformat()
                if coverage_end is not None
                else None
            ),
            "payload": payload,
            "rule_version": rule_version,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StoredAccount:
    account_id: str
    account_type: AccountType
    phase: AccountPhase
    style: AccountStyle
    snapshot: AccountSnapshot
    ftmo_day: str


@dataclass(frozen=True)
class CalendarSnapshot:
    calendar_type: str
    fetched_at: datetime
    coverage_start: datetime | None
    coverage_end: datetime | None
    content_hash: str
    payload: list[dict[str, Any]]
    rule_version: str
    created_at: datetime


@dataclass(frozen=True)
class CredentialRecord:
    credential_id: str
    account_id: str
    not_before: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    scopes: tuple[str, ...]
    created_at: datetime


class StateStore:
    def __init__(
        self,
        path: str | Path,
        day_timezone: str = "Europe/Prague",
    ):
        raw_path = str(path)
        self.path = (
            raw_path
            if raw_path == ":memory:"
            else str(self._absolute_path(raw_path))
        )
        self.day_timezone = day_timezone
        self._lock = threading.RLock()
        self._last_integrity_check_monotonic = float("-inf")
        self._last_integrity_check_ok = False
        self._memory_connection: sqlite3.Connection | None = None
        self._memory_uri: str | None = None
        if self.path == ":memory:":
            self._memory_uri = (
                f"file:ftmo-risk-{id(self)}?mode=memory&cache=shared"
            )
            self._memory_connection = sqlite3.connect(
                self._memory_uri,
                uri=True,
                timeout=5,
                check_same_thread=False,
            )
            self._memory_connection.row_factory = sqlite3.Row
        else:
            self._assert_no_symlink_components(
                self.path,
                "SQLite database",
            )
        database_parent = Path(self.path).parent
        if self.path != ":memory:":
            self._assert_no_symlink_components(
                database_parent,
                "SQLite database directory",
            )
        database_parent.mkdir(parents=True, exist_ok=True)
        if self.path != ":memory:":
            self._assert_no_symlink_components(
                self.path,
                "SQLite database",
            )
        self._assert_directory(database_parent, "SQLite database directory")
        self._secure_database_file()
        self._initialize()

    def _secure_database_file(self) -> None:
        if self.path == ":memory:":
            return
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(self.path, 0o600)
        finally:
            os.close(descriptor)

    def _secure_database_sidecars(self) -> None:
        if self.path == ":memory:":
            return
        for candidate in (self.path, self.path + "-wal", self.path + "-shm"):
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(
                    f"SQLite sidecar must not be a symbolic link: {candidate}"
                )
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"SQLite sidecar must be a regular file: {candidate}"
                )
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(candidate, flags)
            except FileNotFoundError:
                continue
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                else:
                    os.chmod(candidate, 0o600)
            finally:
                os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        if self._memory_uri is not None and self._memory_connection is None:
            raise RuntimeError("state store is closed")
        if self._memory_connection is None:
            # Inspect before sqlite3.connect so a path swapped to a symlink
            # cannot be opened before the safety check runs.
            self._secure_database_sidecars()
        if self._memory_connection is not None:
            connection = self._memory_connection
        else:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
        connection.execute(
            "PRAGMA journal_mode=MEMORY"
            if self._memory_connection is not None
            else "PRAGMA journal_mode=WAL"
        )
        connection.execute("PRAGMA foreign_keys=ON")
        self._secure_database_sidecars()
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection.in_transaction:
                connection.rollback()
            if self._memory_connection is None:
                connection.close()

    def close(self) -> None:
        with self._lock:
            if self._memory_connection is not None:
                self._memory_connection.close()
                self._memory_connection = None

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            schema_row = connection.execute(
                "PRAGMA user_version"
            ).fetchone()
            schema_version = int(schema_row[0]) if schema_row else 0
            if schema_version > CURRENT_SCHEMA_VERSION:
                raise ValueError(
                    "state database schema version is newer than this "
                    f"service (found {schema_version}, supported "
                    f"{CURRENT_SCHEMA_VERSION})"
                )
            if schema_version == CURRENT_SCHEMA_VERSION:
                missing_schema_objects = _missing_required_schema_objects(
                    connection
                )
                if missing_schema_objects:
                    raise ValueError(
                        "state database is marked with the current schema "
                        "version but is missing required risk state objects: "
                        + ", ".join(missing_schema_objects)
                    )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    account_type TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    style TEXT NOT NULL,
                    initial_capital TEXT NOT NULL,
                    ftmo_day TEXT NOT NULL,
                    day_start_balance TEXT NOT NULL,
                    highest_settled_balance TEXT NOT NULL,
                    balance TEXT NOT NULL,
                    equity TEXT NOT NULL,
                    current_open_risk TEXT NOT NULL,
                    reserved_open_risk TEXT NOT NULL DEFAULT '0',
                    open_positions_count INTEGER,
                    pending_orders_count INTEGER,
                    as_of TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data_uncertain INTEGER NOT NULL DEFAULT 0,
                    day_locked INTEGER NOT NULL DEFAULT 0,
                    breach_latched INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    UNIQUE(account_id, kind, request_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );

                CREATE INDEX IF NOT EXISTS activity_account_time
                    ON activity(account_id, occurred_at);
                CREATE INDEX IF NOT EXISTS activity_account_kind_time
                    ON activity(account_id, kind, occurred_at);

                CREATE TABLE IF NOT EXISTS daily_settlements (
                    account_id TEXT NOT NULL,
                    ftmo_day TEXT NOT NULL,
                    settled_balance TEXT NOT NULL,
                    settled_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(account_id, ftmo_day),
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    account_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    reservation_risk TEXT NOT NULL DEFAULT '0',
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, request_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );

                CREATE TABLE IF NOT EXISTS executions (
                    account_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT 'unknown',
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, request_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );

                CREATE INDEX IF NOT EXISTS executions_account_outcome
                    ON executions(account_id, outcome);

                CREATE TABLE IF NOT EXISTS risk_reservations (
                    account_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    reserved_risk TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'unknown', 'committed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    execution_at TEXT,
                    PRIMARY KEY(account_id, request_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );

                CREATE INDEX IF NOT EXISTS risk_reservations_account_status
                    ON risk_reservations(account_id, status, updated_at);

                CREATE TABLE IF NOT EXISTS calendar_snapshots (
                    calendar_type TEXT PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
                    coverage_start TEXT,
                    coverage_end TEXT,
                    hash_version INTEGER NOT NULL DEFAULT 2,
                    content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_credentials (
                    credential_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    secret_salt BLOB NOT NULL,
                    secret_hash BLOB NOT NULL,
                    not_before TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    last_used_at TEXT,
                    scopes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS account_credentials_account
                    ON account_credentials(account_id, expires_at);

                CREATE TABLE IF NOT EXISTS closed_trades (
                    account_id TEXT NOT NULL,
                    trade_id TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'evaluation',
                    cycle_id TEXT NOT NULL DEFAULT 'default',
                    closed_at TEXT NOT NULL,
                    ftmo_day TEXT NOT NULL,
                    net_profit TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    request_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, trade_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );

                CREATE INDEX IF NOT EXISTS closed_trades_account_day
                    ON closed_trades(
                        account_id, phase, cycle_id, ftmo_day
                    );

                CREATE TABLE IF NOT EXISTS qualification_history_status (
                    account_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    history_start_at TEXT NOT NULL,
                    complete_through TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );

                CREATE TABLE IF NOT EXISTS qualification_trading_days (
                    account_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    ftmo_day TEXT NOT NULL,
                    first_opened_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, phase, cycle_id, ftmo_day),
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
                );

                CREATE TABLE IF NOT EXISTS rule_config_fingerprints (
                    rule_version TEXT PRIMARY KEY,
                    config_fingerprint TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS backup_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(accounts)"
                ).fetchall()
            }
            if "data_uncertain" not in columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN data_uncertain "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "day_locked" not in columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN day_locked "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "breach_latched" not in columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN breach_latched "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "open_positions_count" not in columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN open_positions_count "
                    "INTEGER"
                )
            if "pending_orders_count" not in columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN pending_orders_count "
                    "INTEGER"
                )
            if "reserved_open_risk" not in columns:
                connection.execute(
                    "ALTER TABLE accounts ADD COLUMN reserved_open_risk "
                    "TEXT NOT NULL DEFAULT '0'"
                )
            calendar_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(calendar_snapshots)"
                ).fetchall()
            }
            if "coverage_start" not in calendar_columns:
                connection.execute(
                    "ALTER TABLE calendar_snapshots ADD COLUMN coverage_start "
                    "TEXT"
                )
            if "coverage_end" not in calendar_columns:
                connection.execute(
                    "ALTER TABLE calendar_snapshots ADD COLUMN coverage_end "
                    "TEXT"
                )
            if "hash_version" not in calendar_columns:
                connection.execute(
                    "ALTER TABLE calendar_snapshots ADD COLUMN hash_version "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            execution_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(executions)"
                ).fetchall()
            }
            decision_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(decisions)"
                ).fetchall()
            }
            if "reservation_risk" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN reservation_risk "
                    "TEXT NOT NULL DEFAULT '0'"
                )
            if "action" not in execution_columns:
                connection.execute(
                    "ALTER TABLE executions ADD COLUMN action TEXT NOT NULL "
                    "DEFAULT ''"
                )
            if "symbol" not in execution_columns:
                connection.execute(
                    "ALTER TABLE executions ADD COLUMN symbol TEXT NOT NULL "
                    "DEFAULT ''"
                )
            if "outcome" not in execution_columns:
                connection.execute(
                    "ALTER TABLE executions ADD COLUMN outcome TEXT NOT NULL "
                    "DEFAULT 'unknown'"
                )
            closed_trade_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(closed_trades)"
                ).fetchall()
            }
            if "phase" not in closed_trade_columns:
                connection.execute(
                    "ALTER TABLE closed_trades ADD COLUMN phase TEXT NOT NULL "
                    "DEFAULT 'evaluation'"
                )
            if "cycle_id" not in closed_trade_columns:
                connection.execute(
                    "ALTER TABLE closed_trades ADD COLUMN cycle_id TEXT NOT NULL "
                    "DEFAULT 'default'"
                )
            connection.execute(
                "DROP INDEX IF EXISTS closed_trades_account_day"
            )
            connection.execute(
                """
                CREATE INDEX closed_trades_account_day
                ON closed_trades(account_id, phase, cycle_id, ftmo_day)
                """
            )
            missing_schema_objects = _missing_required_schema_objects(
                connection
            )
            if missing_schema_objects:
                raise RuntimeError(
                    "state database migration did not produce required risk "
                    "state objects: "
                    + ", ".join(missing_schema_objects)
                )
            connection.execute(
                f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}"
            )
            connection.commit()

    @staticmethod
    def _credential_digest(secret: str, salt: bytes) -> bytes:
        return hashlib.sha256(salt + secret.encode("utf-8")).digest()

    @staticmethod
    def _utc_timestamp(value: datetime, field: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _credential_record_from_row(
        row: sqlite3.Row,
    ) -> CredentialRecord:
        return CredentialRecord(
            credential_id=row["credential_id"],
            account_id=row["account_id"],
            not_before=datetime.fromisoformat(row["not_before"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            revoked_at=(
                datetime.fromisoformat(row["revoked_at"])
                if row["revoked_at"]
                else None
            ),
            last_used_at=(
                datetime.fromisoformat(row["last_used_at"])
                if row["last_used_at"]
                else None
            ),
            scopes=tuple(json.loads(row["scopes_json"])),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _reservation_total_from_rows(rows: list[sqlite3.Row]) -> Decimal:
        total = ZERO
        for row in rows:
            value = Decimal(row["reserved_risk"])
            if not value.is_finite() or value < ZERO:
                raise ValueError("persisted reservation risk is invalid")
            total += value
        return total

    @staticmethod
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

    @staticmethod
    def _assert_directory(path: Path, label: str) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ValueError(f"unable to inspect {label}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} must not be a symbolic link")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} must be a directory")

    @staticmethod
    def _absolute_path(path: str | Path) -> Path:
        return Path(os.path.abspath(os.path.expanduser(str(path))))

    @classmethod
    def _assert_no_symlink_components(
        cls,
        path: str | Path,
        label: str,
    ) -> None:
        """Reject user-controlled symlink components before file I/O.

        macOS exposes a few system directories through stable aliases, most
        notably ``/var`` and ``/tmp``.  Those aliases are allowed only when
        they point to Apple's corresponding ``/private`` directory; every
        other symlink in the requested path remains rejected.
        """
        allowed_system_aliases = {
            Path("/var"): Path("/private/var"),
            Path("/tmp"): Path("/private/tmp"),
        }
        absolute = cls._absolute_path(path)
        current = Path(absolute.anchor) if absolute.anchor else Path(".")
        start = 1 if absolute.anchor else 0
        for part in absolute.parts[start:]:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ValueError(f"unable to inspect {label} path") from exc
            if stat.S_ISLNK(metadata.st_mode):
                target = allowed_system_aliases.get(current)
                if target is not None:
                    link_target = Path(os.readlink(current))
                    if not link_target.is_absolute():
                        link_target = current.parent / link_target
                    link_target = Path(os.path.abspath(str(link_target)))
                    if link_target == target:
                        continue
                raise ValueError(
                    f"{label} path component must not be a symbolic link: "
                    f"{current}"
                )
            if current != absolute and not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(
                    f"{label} path component must be a directory: {current}"
                )

    @classmethod
    def _checkpoint_file(cls, path: Path) -> None:
        cls._assert_regular_or_missing(path, "SQLite database")
        with closing(sqlite3.connect(str(path), timeout=5)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            cls._assert_regular_or_missing(
                sidecar,
                f"SQLite database {suffix} sidecar",
            )
            sidecar.unlink(missing_ok=True)

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        # Directory fsync is supported on POSIX systems but not uniformly on
        # every platform. The file itself is still durable when this is not
        # available.
        try:
            descriptor = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                return
        finally:
            os.close(descriptor)

    @classmethod
    def _displace_destination_sidecars(
        cls,
        destination: Path,
    ) -> list[tuple[Path, Path]]:
        displaced: list[tuple[Path, Path]] = []
        try:
            for suffix in ("-wal", "-shm"):
                sidecar = destination.with_name(destination.name + suffix)
                cls._assert_regular_or_missing(
                    sidecar,
                    f"backup destination {suffix} sidecar",
                )
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
            cls._restore_destination_sidecars(displaced)
            raise
        return displaced

    @staticmethod
    def _restore_destination_sidecars(
        displaced: list[tuple[Path, Path]],
    ) -> None:
        for original, temporary in reversed(displaced):
            if not temporary.exists() or original.exists():
                continue
            os.replace(temporary, original)

    @staticmethod
    def _discard_destination_sidecars(
        displaced: list[tuple[Path, Path]],
    ) -> None:
        for _, temporary in displaced:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _active_reservation_risk_connection(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> Decimal:
        placeholders = ",".join("?" for _ in ACTIVE_RESERVATION_STATUSES)
        rows = connection.execute(
            "SELECT reserved_risk FROM risk_reservations "
            f"WHERE account_id = ? AND status IN ({placeholders})",
            (account_id, *ACTIVE_RESERVATION_STATUSES),
        ).fetchall()
        return self._reservation_total_from_rows(rows)

    def _refresh_reserved_risk_connection(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> Decimal:
        total = self._active_reservation_risk_connection(
            connection,
            account_id,
        )
        connection.execute(
            "UPDATE accounts SET reserved_open_risk = ? "
            "WHERE account_id = ?",
            (str(total), account_id),
        )
        return total

    def _release_committed_reservations_connection(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        as_of: datetime,
    ) -> None:
        connection.execute(
            """
            DELETE FROM risk_reservations
            WHERE account_id = ?
              AND status = 'committed'
              AND execution_at IS NOT NULL
              AND execution_at <= ?
            """,
            (account_id, as_of.astimezone(timezone.utc).isoformat()),
        )
        self._refresh_reserved_risk_connection(connection, account_id)

    @staticmethod
    def _blocked_replay_response(
        response_json: str,
        *,
        code: str,
        reason: str,
        execution_outcome: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = json.loads(response_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("persisted decision response is invalid") from exc
        if not isinstance(response, dict):
            raise ValueError("persisted decision response must be an object")
        decision = response.get("decision")
        if not isinstance(decision, dict):
            raise ValueError("persisted decision response has no decision")
        blocked_decision = dict(decision)
        blocked_decision.update(
            {
                "code": code,
                "allowed": False,
                "reasons": [reason],
            }
        )
        response["decision"] = blocked_decision
        response["replayed"] = True
        if execution_outcome is not None:
            response["execution_outcome"] = execution_outcome
        return response

    def _promote_expired_pending_reservations_connection(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        now: datetime,
    ) -> int:
        cutoff = (now - PENDING_RESERVATION_LEASE).isoformat()
        rows = connection.execute(
            """
            SELECT account_id, request_id, request_hash, action, symbol,
                   reserved_risk, created_at
            FROM risk_reservations
            WHERE account_id = ? AND status = 'pending' AND created_at <= ?
            ORDER BY created_at, request_id
            """,
            (account_id, cutoff),
        ).fetchall()
        promoted = 0
        for row in rows:
            existing = connection.execute(
                """
                SELECT outcome
                FROM executions
                WHERE account_id = ? AND request_id = ?
                """,
                (row["account_id"], row["request_id"]),
            ).fetchone()
            if existing is not None and existing["outcome"] == "failure":
                connection.execute(
                    """
                    DELETE FROM activity
                    WHERE account_id = ? AND kind = ? AND request_id = ?
                    """,
                    (account_id, row["action"], row["request_id"]),
                )
                connection.execute(
                    """
                    DELETE FROM risk_reservations
                    WHERE account_id = ? AND request_id = ?
                    """,
                    (account_id, row["request_id"]),
                )
                promoted += 1
                continue
            if existing is not None and existing["outcome"] == "success":
                connection.execute(
                    """
                    UPDATE risk_reservations
                    SET status = 'committed', execution_at = ?, updated_at = ?
                    WHERE account_id = ? AND request_id = ?
                    """,
                    (
                        now.isoformat(),
                        now.isoformat(),
                        account_id,
                        row["request_id"],
                    ),
                )
                promoted += 1
                continue

            if existing is None:
                response = {
                    "ok": True,
                    "account_id": account_id,
                    "request_id": row["request_id"],
                    "execution_recorded": True,
                    "outcome": "unknown",
                    "reservation_released": False,
                    "reservation_committed": False,
                    "resolved_unknown": False,
                    "lease_expired": True,
                }
                connection.execute(
                    """
                    INSERT INTO executions (
                        account_id, request_id, request_hash, action, symbol,
                        outcome, response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?)
                    """,
                    (
                        account_id,
                        row["request_id"],
                        row["request_hash"],
                        row["action"],
                        row["symbol"],
                        json.dumps(
                            response,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now.isoformat(),
                    ),
                )
            connection.execute(
                """
                UPDATE risk_reservations
                SET status = 'unknown', updated_at = ?
                WHERE account_id = ? AND request_id = ?
                """,
                (now.isoformat(), account_id, row["request_id"]),
            )
            self._insert_activity_connection(
                connection,
                account_id=account_id,
                kind="execution",
                symbol=row["symbol"],
                occurred_at=now,
                request_id=row["request_id"],
                detail="reservation lease expired before execution result",
            )
            promoted += 1
        if rows:
            self._refresh_reserved_risk_connection(connection, account_id)
        return promoted

    def _load_account_connection(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        now: datetime,
    ) -> tuple[sqlite3.Row | None, Decimal]:
        self._promote_expired_pending_reservations_connection(
            connection,
            account_id,
            now,
        )
        row = connection.execute(
            "SELECT * FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return None, ZERO
        reserved_open_risk = self._active_reservation_risk_connection(
            connection,
            account_id,
        )
        stored_value = row["reserved_open_risk"]
        try:
            stored_reserved = Decimal(stored_value)
        except (TypeError, ValueError, ArithmeticError):
            stored_reserved = None
        if stored_reserved != reserved_open_risk:
            connection.execute(
                "UPDATE accounts SET reserved_open_risk = ? "
                "WHERE account_id = ?",
                (str(reserved_open_risk), account_id),
            )
        return row, reserved_open_risk

    def _unknown_execution_count_connection(
        self,
        connection: sqlite3.Connection,
        account_id: str | None = None,
    ) -> int:
        if account_id is None:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM (
                    SELECT account_id, request_id
                    FROM executions
                    WHERE outcome = 'unknown'
                    UNION
                    SELECT account_id, request_id
                    FROM risk_reservations
                    WHERE status = 'unknown'
                )
                """
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM (
                    SELECT request_id
                    FROM executions
                    WHERE account_id = ? AND outcome = 'unknown'
                    UNION
                    SELECT request_id
                    FROM risk_reservations
                    WHERE account_id = ? AND status = 'unknown'
                )
                """,
                (account_id, account_id),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def reconcile_expired_reservations(
        self,
        account_id: str | None = None,
    ) -> int:
        """Promote abandoned pending executions to an auditable unknown state."""
        now = datetime.now(timezone.utc)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if account_id is None:
                rows = connection.execute(
                    "SELECT account_id FROM accounts ORDER BY account_id"
                ).fetchall()
                account_ids = [str(row["account_id"]) for row in rows]
            else:
                account_ids = [account_id]
            promoted = sum(
                self._promote_expired_pending_reservations_connection(
                    connection,
                    item,
                    now,
                )
                for item in account_ids
            )
            connection.commit()
        return promoted

    def save_calendar_snapshot(
        self,
        *,
        calendar_type: str,
        fetched_at: datetime,
        coverage_start: datetime | None = None,
        coverage_end: datetime | None = None,
        payload: list[dict[str, Any]],
        rule_version: str,
    ) -> CalendarSnapshot:
        if calendar_type not in {"news", "market"}:
            raise ValueError("calendar_type must be news or market")
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        if not isinstance(payload, list):
            raise ValueError("calendar payload must be a list")
        if not rule_version.strip():
            raise ValueError("rule_version must be non-empty")
        if (coverage_start is None) != (coverage_end is None):
            raise ValueError(
                "calendar coverage_start and coverage_end must be supplied together"
            )
        coverage_start_utc: datetime | None = None
        coverage_end_utc: datetime | None = None
        if coverage_start is not None and coverage_end is not None:
            if coverage_start.tzinfo is None or coverage_end.tzinfo is None:
                raise ValueError("calendar coverage timestamps must be timezone-aware")
            coverage_start_utc = coverage_start.astimezone(timezone.utc)
            coverage_end_utc = coverage_end.astimezone(timezone.utc)
            if coverage_end_utc <= coverage_start_utc:
                raise ValueError("calendar coverage_end must be after coverage_start")
        payload_json = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        fetched_at_utc = fetched_at.astimezone(timezone.utc)
        content_hash = _calendar_content_hash(
            calendar_type=calendar_type,
            fetched_at=fetched_at_utc,
            coverage_start=coverage_start_utc,
            coverage_end=coverage_end_utc,
            payload=payload,
            rule_version=rule_version,
        )
        created_at = datetime.now(timezone.utc)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                """
                SELECT * FROM calendar_snapshots
                WHERE calendar_type = ?
                """,
                (calendar_type,),
            ).fetchone()
            if existing is not None:
                existing_at = datetime.fromisoformat(existing["fetched_at"])
                if fetched_at_utc < existing_at:
                    raise ValueError(
                        f"{calendar_type} calendar update is older than "
                        "the persisted update"
                    )
                if (
                    fetched_at_utc == existing_at
                    and (
                        existing["content_hash"] != content_hash
                        or existing["rule_version"] != rule_version
                        or existing["coverage_start"]
                        != (
                            coverage_start_utc.isoformat()
                            if coverage_start_utc is not None
                            else None
                        )
                        or existing["coverage_end"]
                        != (
                            coverage_end_utc.isoformat()
                            if coverage_end_utc is not None
                            else None
                        )
                    )
                ):
                    raise ValueError(
                        f"{calendar_type} calendar timestamp already has "
                        "different persisted content"
                    )
            connection.execute(
                """
                INSERT INTO calendar_snapshots (
                    calendar_type, fetched_at, coverage_start, coverage_end,
                    hash_version, content_hash, payload_json, rule_version,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_type) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    coverage_start = excluded.coverage_start,
                    coverage_end = excluded.coverage_end,
                    hash_version = excluded.hash_version,
                    content_hash = excluded.content_hash,
                    payload_json = excluded.payload_json,
                    rule_version = excluded.rule_version,
                    created_at = excluded.created_at
                """,
                (
                    calendar_type,
                    fetched_at_utc.isoformat(),
                    (
                        coverage_start_utc.isoformat()
                        if coverage_start_utc is not None
                        else None
                    ),
                    (
                        coverage_end_utc.isoformat()
                        if coverage_end_utc is not None
                        else None
                    ),
                    CALENDAR_HASH_VERSION,
                    content_hash,
                    payload_json,
                    rule_version,
                    created_at.isoformat(),
                ),
            )
            connection.commit()
        return CalendarSnapshot(
            calendar_type=calendar_type,
            fetched_at=fetched_at_utc,
            coverage_start=coverage_start_utc,
            coverage_end=coverage_end_utc,
            content_hash=content_hash,
            payload=payload,
            rule_version=rule_version,
            created_at=created_at,
        )

    def get_calendar_snapshots(self) -> dict[str, CalendarSnapshot]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM calendar_snapshots"
            ).fetchall()
        result: dict[str, CalendarSnapshot] = {}
        for row in rows:
            payload_json = row["payload_json"]
            payload = json.loads(payload_json)
            if not isinstance(payload, list):
                raise ValueError("persisted calendar payload must be a list")
            fetched_at = datetime.fromisoformat(row["fetched_at"])
            if fetched_at.tzinfo is None:
                raise ValueError("persisted calendar fetched_at needs a timezone")
            coverage_start = (
                datetime.fromisoformat(row["coverage_start"])
                if row["coverage_start"]
                else None
            )
            coverage_end = (
                datetime.fromisoformat(row["coverage_end"])
                if row["coverage_end"]
                else None
            )
            if (coverage_start is None) != (coverage_end is None):
                raise ValueError(
                    "persisted calendar coverage bounds are incomplete"
                )
            if coverage_start is not None and coverage_start.tzinfo is None:
                raise ValueError(
                    "persisted calendar coverage_start needs a timezone"
                )
            if coverage_end is not None and coverage_end.tzinfo is None:
                raise ValueError(
                    "persisted calendar coverage_end needs a timezone"
                )
            if (
                coverage_start is not None
                and coverage_end is not None
                and coverage_end <= coverage_start
            ):
                raise ValueError("persisted calendar coverage bounds are invalid")
            hash_version = int(row["hash_version"])
            if hash_version == 1:
                if coverage_start is not None or coverage_end is not None:
                    raise ValueError(
                        "legacy calendar snapshots cannot declare trusted coverage"
                    )
                content_hash = hashlib.sha256(
                    payload_json.encode("utf-8")
                ).hexdigest()
            elif hash_version == CALENDAR_HASH_VERSION:
                content_hash = _calendar_content_hash(
                    calendar_type=row["calendar_type"],
                    fetched_at=fetched_at,
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    payload=payload,
                    rule_version=row["rule_version"],
                )
            else:
                raise ValueError("persisted calendar hash version is unsupported")
            if not compare_digest(content_hash, row["content_hash"]):
                raise ValueError(
                    f"persisted {row['calendar_type']} calendar content hash "
                    "does not match its safety metadata"
                )
            result[row["calendar_type"]] = CalendarSnapshot(
                calendar_type=row["calendar_type"],
                fetched_at=fetched_at,
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                content_hash=content_hash,
                payload=payload,
                rule_version=row["rule_version"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        return result

    def pin_rule_config_fingerprint(
        self,
        *,
        rule_version: str,
        config_fingerprint: str,
        now: datetime | None = None,
    ) -> bool:
        """Pin one risk-rule fingerprint to each declared rule version.

        A config revision must use a new rule_version. Returning ``False``
        keeps the API available for inspection while forcing its risk gate to
        fail closed.
        """

        if not isinstance(rule_version, str) or not rule_version.strip():
            raise ValueError("rule_version must be non-empty")
        if (
            not isinstance(config_fingerprint, str)
            or len(config_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in config_fingerprint)
        ):
            raise ValueError("config_fingerprint must be a SHA-256 hex digest")
        recorded_at = self._utc_timestamp(
            now or datetime.now(timezone.utc),
            "now",
        )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT config_fingerprint
                FROM rule_config_fingerprints
                WHERE rule_version = ?
                """,
                (rule_version,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO rule_config_fingerprints (
                        rule_version, config_fingerprint, recorded_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        rule_version,
                        config_fingerprint,
                        recorded_at.isoformat(),
                    ),
                )
                connection.commit()
                return True
            matches = compare_digest(
                str(existing["config_fingerprint"]),
                config_fingerprint,
            )
            connection.commit()
            return matches

    def create_account_credential(
        self,
        *,
        account_id: str,
        scopes: tuple[str, ...] | list[str],
        not_before: datetime,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> tuple[CredentialRecord, str]:
        if not account_id:
            raise ValueError("account_id must be non-empty")
        if not scopes:
            raise ValueError("at least one credential scope is required")
        not_before = self._utc_timestamp(not_before, "not_before")
        expires_at = self._utc_timestamp(expires_at, "expires_at")
        now = self._utc_timestamp(
            now or datetime.now(timezone.utc),
            "now",
        )
        if expires_at <= not_before:
            raise ValueError("expires_at must be after not_before")
        if any(not isinstance(scope, str) or not scope.strip() for scope in scopes):
            raise ValueError("credential scopes must contain non-empty strings")
        credential_id = uuid4().hex
        secret = f"rsk_{credential_id}.{secrets.token_urlsafe(32)}"
        salt = secrets.token_bytes(16)
        digest = self._credential_digest(secret, salt)
        created_at = now
        scopes_tuple = tuple(sorted({scope.strip() for scope in scopes}))
        if any(scope not in ACCOUNT_CREDENTIAL_SCOPES for scope in scopes_tuple):
            raise ValueError("credential scopes contain an unsupported scope")
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO account_credentials (
                    credential_id, account_id, secret_salt, secret_hash,
                    not_before, expires_at, scopes_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    account_id,
                    salt,
                    digest,
                    not_before.isoformat(),
                    expires_at.isoformat(),
                    json.dumps(scopes_tuple, ensure_ascii=True),
                    created_at.isoformat(),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM account_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("credential was not created")
        return self._credential_record_from_row(row), secret

    def authenticate_account_credential(
        self,
        *,
        account_id: str,
        secret: str,
        scope: str,
        now: datetime | None = None,
    ) -> CredentialRecord | None:
        if (
            not account_id
            or not isinstance(secret, str)
            or not secret
            or len(secret) > MAX_CREDENTIAL_SECRET_LENGTH
            or not scope
        ):
            return None
        prefix, separator, _ = secret.partition(".")
        if separator != "." or not prefix.startswith("rsk_"):
            return None
        credential_id = prefix[4:]
        if len(credential_id) != 32:
            return None
        try:
            now = self._utc_timestamp(
                now or datetime.now(timezone.utc),
                "now",
            )
        except ValueError:
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM account_credentials
                WHERE account_id = ? AND credential_id = ?
                """,
                (account_id, credential_id),
            ).fetchone()
            if row is None:
                return None
            try:
                record = self._credential_record_from_row(row)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if any(
                item not in ACCOUNT_CREDENTIAL_SCOPES
                for item in record.scopes
            ):
                return None
            valid = (
                record.not_before.astimezone(timezone.utc) <= now
                and record.expires_at.astimezone(timezone.utc) > now
                and (
                    record.revoked_at is None
                    or record.revoked_at.astimezone(timezone.utc) > now
                )
                and scope in record.scopes
            )
            digest = self._credential_digest(
                secret,
                bytes(row["secret_salt"]),
            )
            if not valid or not compare_digest(
                digest,
                bytes(row["secret_hash"]),
            ):
                return None
            last_used_at = (
                record.last_used_at.astimezone(timezone.utc)
                if record.last_used_at is not None
                else None
            )
            if (
                last_used_at is None
                or now - last_used_at >= CREDENTIAL_LAST_USED_WRITE_INTERVAL
            ):
                connection.execute(
                    """
                    UPDATE account_credentials
                    SET last_used_at = ?
                    WHERE credential_id = ?
                    """,
                    (now.isoformat(), record.credential_id),
                )
                connection.commit()
                return replace(record, last_used_at=now)
            return record
        return None

    def rotate_account_credential(
        self,
        *,
        credential_id: str,
        scopes: tuple[str, ...] | list[str] | None,
        not_before: datetime,
        expires_at: datetime,
        overlap_seconds: int = 0,
        now: datetime | None = None,
    ) -> tuple[CredentialRecord, str]:
        if isinstance(overlap_seconds, bool) or not isinstance(overlap_seconds, int):
            raise ValueError("overlap_seconds must be an integer")
        if overlap_seconds < 0:
            raise ValueError("overlap_seconds cannot be negative")
        now = self._utc_timestamp(now or datetime.now(timezone.utc), "now")
        not_before = self._utc_timestamp(not_before, "not_before")
        expires_at = self._utc_timestamp(expires_at, "expires_at")
        if expires_at <= not_before:
            raise ValueError("expires_at must be after not_before")
        new_credential_id = uuid4().hex
        new_secret = (
            f"rsk_{new_credential_id}.{secrets.token_urlsafe(32)}"
        )
        salt = secrets.token_bytes(16)
        digest = self._credential_digest(new_secret, salt)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT account_id, scopes_json, revoked_at
                FROM account_credentials
                WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown credential_id: {credential_id}")
            selected_scopes = (
                tuple(json.loads(row["scopes_json"]))
                if scopes is None
                else tuple(scopes)
            )
            if not selected_scopes:
                raise ValueError("at least one credential scope is required")
            if any(
                not isinstance(scope, str) or not scope.strip()
                for scope in selected_scopes
            ):
                raise ValueError(
                    "credential scopes must contain non-empty strings"
                )
            if any(
                scope not in ACCOUNT_CREDENTIAL_SCOPES
                for scope in selected_scopes
            ):
                raise ValueError("credential scopes contain an unsupported scope")
            requested_revoked_at = now + timedelta(seconds=overlap_seconds)
            existing_revoked_at = (
                datetime.fromisoformat(row["revoked_at"])
                .astimezone(timezone.utc)
                if row["revoked_at"]
                else None
            )
            # Rotation must never reactivate a credential that was already
            # revoked. A future revocation may be extended, but never moved
            # earlier than the existing deadline.
            if existing_revoked_at is not None and existing_revoked_at <= now:
                effective_revoked_at = existing_revoked_at
            elif existing_revoked_at is not None:
                effective_revoked_at = max(
                    existing_revoked_at,
                    requested_revoked_at,
                )
            else:
                effective_revoked_at = requested_revoked_at
            connection.execute(
                """
                INSERT INTO account_credentials (
                    credential_id, account_id, secret_salt, secret_hash,
                    not_before, expires_at, scopes_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_credential_id,
                    row["account_id"],
                    salt,
                    digest,
                    not_before.isoformat(),
                    expires_at.isoformat(),
                    json.dumps(
                        tuple(sorted(set(selected_scopes))),
                        ensure_ascii=True,
                    ),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE account_credentials
                SET revoked_at = ?
                WHERE credential_id = ?
                  AND (revoked_at IS NULL OR revoked_at < ?)
                """,
                (
                    effective_revoked_at.isoformat(),
                    credential_id,
                    effective_revoked_at.isoformat(),
                ),
            )
            connection.commit()
            new_row = connection.execute(
                """
                SELECT * FROM account_credentials
                WHERE credential_id = ?
                """,
                (new_credential_id,),
            ).fetchone()
        if new_row is None:
            raise RuntimeError("rotated credential was not created")
        return self._credential_record_from_row(new_row), new_secret

    def revoke_account_credential(
        self,
        credential_id: str,
        revoked_at: datetime | None = None,
    ) -> CredentialRecord:
        revoked_at = self._utc_timestamp(
            revoked_at or datetime.now(timezone.utc),
            "revoked_at",
        )
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT revoked_at FROM account_credentials "
                "WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"unknown credential_id: {credential_id}")
            existing_revoked_at = (
                datetime.fromisoformat(existing["revoked_at"])
                if existing["revoked_at"]
                else None
            )
            effective_revoked_at = revoked_at
            if existing_revoked_at is not None:
                effective_revoked_at = max(
                    existing_revoked_at.astimezone(timezone.utc),
                    revoked_at,
                )
            connection.execute(
                """
                UPDATE account_credentials
                SET revoked_at = ?
                WHERE credential_id = ?
                """,
                (effective_revoked_at.isoformat(), credential_id),
            )
            row = connection.execute(
                "SELECT * FROM account_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("credential disappeared during revocation")
        return self._credential_record_from_row(row)

    def list_account_credentials(
        self,
        account_id: str | None = None,
    ) -> list[CredentialRecord]:
        with self._lock, self._connection() as connection:
            if account_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM account_credentials
                    ORDER BY account_id, created_at DESC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM account_credentials
                    WHERE account_id = ?
                    ORDER BY created_at DESC
                    """,
                    (account_id,),
                ).fetchall()
        return [self._credential_record_from_row(row) for row in rows]

    def credential_metrics(
        self,
        *,
        now: datetime | None = None,
        expiring_within_seconds: int = 86400,
    ) -> dict[str, int]:
        if expiring_within_seconds < 0:
            raise ValueError("expiring_within_seconds cannot be negative")
        now_utc = self._utc_timestamp(
            now or datetime.now(timezone.utc),
            "now",
        )
        horizon = now_utc + timedelta(seconds=expiring_within_seconds)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT not_before, expires_at, revoked_at
                FROM account_credentials
                """
            ).fetchall()
        counts = {
            "active": 0,
            "expired": 0,
            "expiring_soon": 0,
            "not_yet_active": 0,
        }
        for row in rows:
            revoked_at = (
                datetime.fromisoformat(row["revoked_at"])
                if row["revoked_at"]
                else None
            )
            if revoked_at is not None and revoked_at.astimezone(
                timezone.utc
            ) <= now_utc:
                continue
            not_before = datetime.fromisoformat(row["not_before"]).astimezone(
                timezone.utc
            )
            expires_at = datetime.fromisoformat(row["expires_at"]).astimezone(
                timezone.utc
            )
            if not_before > now_utc:
                counts["not_yet_active"] += 1
            elif expires_at <= now_utc:
                counts["expired"] += 1
            else:
                counts["active"] += 1
                if expires_at <= horizon:
                    counts["expiring_soon"] += 1
        return counts

    def record_closed_trade(
        self,
        *,
        account_id: str,
        trade_id: str,
        phase: AccountPhase,
        cycle_id: str,
        closed_at: datetime,
        ftmo_day: str,
        net_profit: Decimal,
        symbol: str,
        source: str,
        request_id: str,
    ) -> dict[str, Any]:
        if closed_at.tzinfo is None:
            raise ValueError("closed_at must be timezone-aware")
        if not trade_id:
            raise ValueError("trade_id must be non-empty")
        if not cycle_id:
            raise ValueError("cycle_id must be non-empty")
        if not source.strip():
            raise ValueError("source must be non-empty")
        if not ftmo_day:
            raise ValueError("ftmo_day must be non-empty")
        if ftmo_day_key(closed_at, self.day_timezone) != ftmo_day:
            raise ValueError("ftmo_day does not match closed_at")
        if not net_profit.is_finite():
            raise ValueError("net_profit must be finite")
        payload = {
            "account_id": account_id,
            "trade_id": trade_id,
            "phase": phase.value,
            "cycle_id": cycle_id,
            "closed_at": closed_at.astimezone(timezone.utc).isoformat(),
            "ftmo_day": ftmo_day,
            "net_profit": str(net_profit),
            "symbol": symbol,
            "source": source,
            "request_id": request_id,
        }
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT phase FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"unknown account_id: {account_id}")
            phase_order = {
                AccountPhase.EVALUATION.value: 0,
                AccountPhase.VERIFICATION.value: 1,
                AccountPhase.FTMO_ACCOUNT.value: 2,
            }
            if phase_order[phase.value] > phase_order[account["phase"]]:
                raise ValueError(
                    "closed trade phase cannot be ahead of account phase"
                )
            existing = connection.execute(
                """
                SELECT closed_at, ftmo_day, net_profit, symbol, source,
                       request_id, phase, cycle_id
                FROM closed_trades
                WHERE account_id = ? AND trade_id = ?
                """,
                (account_id, trade_id),
            ).fetchone()
            if existing is not None:
                existing_payload = {
                    "account_id": account_id,
                    "trade_id": trade_id,
                    "phase": existing["phase"],
                    "cycle_id": existing["cycle_id"],
                    "closed_at": existing["closed_at"],
                    "ftmo_day": existing["ftmo_day"],
                    "net_profit": existing["net_profit"],
                    "symbol": existing["symbol"],
                    "source": existing["source"],
                    "request_id": existing["request_id"],
                }
                if existing_payload != payload:
                    raise ValueError(
                        "trade_id has already been used with different content"
                    )
                connection.commit()
                return {
                    "ok": True,
                    "account_id": account_id,
                    "trade_id": trade_id,
                    "recorded": False,
                    "idempotent": True,
                }
            connection.execute(
                """
                INSERT INTO closed_trades (
                    account_id, trade_id, phase, cycle_id, closed_at,
                    ftmo_day, net_profit, symbol, source, request_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    trade_id,
                    phase.value,
                    cycle_id,
                    payload["closed_at"],
                    ftmo_day,
                    str(net_profit),
                    symbol,
                    source,
                    request_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        return {
            "ok": True,
            "account_id": account_id,
            "trade_id": trade_id,
            "recorded": True,
            "idempotent": False,
        }

    def closed_trades(
        self,
        account_id: str,
        *,
        phase: AccountPhase | None = None,
        cycle_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            if phase is None and cycle_id is None:
                rows = connection.execute(
                    """
                    SELECT trade_id, phase, cycle_id, closed_at, ftmo_day,
                           net_profit, symbol, source, request_id, created_at
                    FROM closed_trades
                    WHERE account_id = ?
                    ORDER BY closed_at, trade_id
                    """,
                    (account_id,),
                ).fetchall()
            elif phase is not None and cycle_id is not None:
                rows = connection.execute(
                    """
                    SELECT trade_id, phase, cycle_id, closed_at, ftmo_day,
                           net_profit, symbol, source, request_id, created_at
                    FROM closed_trades
                    WHERE account_id = ? AND phase = ? AND cycle_id = ?
                    ORDER BY closed_at, trade_id
                    """,
                    (account_id, phase.value, cycle_id),
                ).fetchall()
            else:
                raise ValueError(
                    "phase and cycle_id must be supplied together"
                )
        return [dict(row) for row in rows]

    def set_qualification_history_status(
        self,
        *,
        account_id: str,
        phase: AccountPhase,
        cycle_id: str,
        history_start_at: datetime,
        complete_through: datetime,
        source: str,
    ) -> dict[str, Any]:
        if history_start_at.tzinfo is None or complete_through.tzinfo is None:
            raise ValueError("qualification history timestamps need timezones")
        if complete_through < history_start_at:
            raise ValueError(
                "complete_through cannot be before history_start_at"
            )
        if not source.strip():
            raise ValueError("source must be non-empty")
        now = datetime.now(timezone.utc)
        start_utc = history_start_at.astimezone(timezone.utc)
        complete_utc = complete_through.astimezone(timezone.utc)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT phase FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"unknown account_id: {account_id}")
            if account["phase"] != phase.value:
                raise ValueError(
                    "qualification history phase must match current account phase"
                )
            existing = connection.execute(
                """
                SELECT phase, cycle_id, history_start_at, complete_through
                FROM qualification_history_status
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["phase"] == phase.value
                and existing["cycle_id"] == cycle_id
            ):
                if datetime.fromisoformat(
                    existing["history_start_at"]
                ) != start_utc:
                    raise ValueError(
                        "history_start_at cannot change within one cycle"
                    )
                if complete_utc < datetime.fromisoformat(
                    existing["complete_through"]
                ):
                    raise ValueError(
                        "qualification history completeness cannot move backwards"
                    )
            connection.execute(
                """
                INSERT INTO qualification_history_status (
                    account_id, phase, cycle_id, history_start_at,
                    complete_through, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    phase = excluded.phase,
                    cycle_id = excluded.cycle_id,
                    history_start_at = excluded.history_start_at,
                    complete_through = excluded.complete_through,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    phase.value,
                    cycle_id,
                    start_utc.isoformat(),
                    complete_utc.isoformat(),
                    source,
                    now.isoformat(),
                ),
            )
            connection.commit()
        return {
            "account_id": account_id,
            "phase": phase.value,
            "cycle_id": cycle_id,
            "history_start_at": start_utc.isoformat(),
            "complete_through": complete_utc.isoformat(),
            "source": source,
            "updated_at": now.isoformat(),
        }

    def record_qualification_trading_day(
        self,
        *,
        account_id: str,
        phase: AccountPhase,
        cycle_id: str,
        opened_at: datetime,
        ftmo_day: str,
        source: str,
        request_id: str,
    ) -> dict[str, Any]:
        if opened_at.tzinfo is None:
            raise ValueError("opened_at must be timezone-aware")
        if not cycle_id:
            raise ValueError("cycle_id must be non-empty")
        if not ftmo_day:
            raise ValueError("ftmo_day must be non-empty")
        if ftmo_day_key(opened_at, self.day_timezone) != ftmo_day:
            raise ValueError("ftmo_day does not match opened_at")
        if not source.strip():
            raise ValueError("source must be non-empty")
        opened_at_utc = opened_at.astimezone(timezone.utc)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute(
                "SELECT phase FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if account is None:
                raise KeyError(f"unknown account_id: {account_id}")
            phase_order = {
                AccountPhase.EVALUATION.value: 0,
                AccountPhase.VERIFICATION.value: 1,
                AccountPhase.FTMO_ACCOUNT.value: 2,
            }
            if phase_order[phase.value] > phase_order[account["phase"]]:
                raise ValueError(
                    "trading-day phase cannot be ahead of account phase"
                )
            existing = connection.execute(
                """
                SELECT first_opened_at, source, request_id
                FROM qualification_trading_days
                WHERE account_id = ? AND phase = ? AND cycle_id = ?
                  AND ftmo_day = ?
                """,
                (account_id, phase.value, cycle_id, ftmo_day),
            ).fetchone()
            recorded = existing is None
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO qualification_trading_days (
                        account_id, phase, cycle_id, ftmo_day,
                        first_opened_at, source, request_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        phase.value,
                        cycle_id,
                        ftmo_day,
                        opened_at_utc.isoformat(),
                        source,
                        request_id,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            elif opened_at_utc < datetime.fromisoformat(
                existing["first_opened_at"]
            ):
                connection.execute(
                    """
                    UPDATE qualification_trading_days
                    SET first_opened_at = ?, source = ?, request_id = ?
                    WHERE account_id = ? AND phase = ? AND cycle_id = ?
                      AND ftmo_day = ?
                    """,
                    (
                        opened_at_utc.isoformat(),
                        source,
                        request_id,
                        account_id,
                        phase.value,
                        cycle_id,
                        ftmo_day,
                    ),
                )
            connection.commit()
        return {
            "account_id": account_id,
            "phase": phase.value,
            "cycle_id": cycle_id,
            "ftmo_day": ftmo_day,
            "first_opened_at": (
                opened_at_utc.isoformat()
                if existing is None
                else min(
                    opened_at_utc,
                    datetime.fromisoformat(existing["first_opened_at"]),
                ).isoformat()
            ),
            "recorded": recorded,
        }

    def qualification_trading_days(
        self,
        account_id: str,
        *,
        phase: AccountPhase,
        cycle_id: str,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT ftmo_day, first_opened_at, source, request_id,
                       created_at
                FROM qualification_trading_days
                WHERE account_id = ? AND phase = ? AND cycle_id = ?
                ORDER BY ftmo_day
                """,
                (account_id, phase.value, cycle_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def qualification_history_status(
        self,
        account_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT phase, cycle_id, history_start_at, complete_through,
                       source, updated_at
                FROM qualification_history_status
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def all_accounts(self) -> list[StoredAccount]:
        now = datetime.now(timezone.utc)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            account_rows = connection.execute(
                "SELECT account_id, reserved_open_risk "
                "FROM accounts ORDER BY account_id"
            ).fetchall()
            reservation_totals: dict[str, Decimal] = {}
            for account_row in account_rows:
                account_id = str(account_row["account_id"])
                self._promote_expired_pending_reservations_connection(
                    connection,
                    account_id,
                    now,
                )
                reservation_totals[account_id] = (
                    self._active_reservation_risk_connection(
                        connection,
                        account_id,
                    )
                )
                try:
                    stored_reserved = Decimal(
                        account_row["reserved_open_risk"]
                    )
                except (TypeError, ValueError, ArithmeticError):
                    stored_reserved = None
                if stored_reserved != reservation_totals[account_id]:
                    connection.execute(
                        "UPDATE accounts SET reserved_open_risk = ? "
                        "WHERE account_id = ?",
                        (str(reservation_totals[account_id]), account_id),
                    )
            rows = connection.execute(
                "SELECT * FROM accounts ORDER BY account_id"
            ).fetchall()
            connection.commit()
        return [
            self._stored_account_from_row(
                row,
                reserved_open_risk=reservation_totals.get(
                    str(row["account_id"]),
                    ZERO,
                ),
            )
            for row in rows
        ]

    def database_healthy(self) -> bool:
        if self.path != ":memory:" and not Path(self.path).is_file():
            return False
        try:
            with self._lock, self._connection() as connection:
                row = connection.execute("SELECT 1").fetchone()
                schema_row = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()
                schema_version = (
                    int(schema_row[0]) if schema_row else None
                )
                missing_schema_objects = _missing_required_schema_objects(
                    connection
                )
                monotonic_now = time.monotonic()
                if (
                    monotonic_now - self._last_integrity_check_monotonic
                    >= DATABASE_INTEGRITY_CHECK_INTERVAL_SECONDS
                ):
                    integrity = connection.execute(
                        "PRAGMA quick_check(1)"
                    ).fetchone()
                    foreign_key_error = connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchone()
                    self._last_integrity_check_ok = bool(
                        integrity
                        and integrity[0] == "ok"
                        and foreign_key_error is None
                    )
                    self._last_integrity_check_monotonic = monotonic_now
            return bool(
                row
                and row[0] == 1
                and schema_version == CURRENT_SCHEMA_VERSION
                and self._last_integrity_check_ok
                and not missing_schema_objects
            )
        except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError):
            self._last_integrity_check_ok = False
            return False

    def unknown_execution_count(self, account_id: str | None = None) -> int:
        with self._lock, self._connection() as connection:
            return self._unknown_execution_count_connection(
                connection,
                account_id,
            )

    def risk_state_integrity_issues(self, *, limit: int = 100) -> list[str]:
        """Return semantic consistency failures in execution state.

        SQLite's structural checks cannot prove that a reservation still
        matches its decision and execution records. This read-only audit is
        intentionally fail-closed: callers should stop new risk when it
        returns any issue instead of trying to repair records silently.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._lock, self._connection() as connection:
            reservation_rows = connection.execute(
                """
                SELECT r.account_id, r.request_id, r.request_hash,
                       r.action, r.symbol, r.reserved_risk, r.status,
                       r.execution_at,
                       d.request_hash AS decision_hash,
                       d.action AS decision_action,
                       d.symbol AS decision_symbol,
                       d.allowed AS decision_allowed,
                       d.reservation_risk AS decision_reservation_risk,
                       e.outcome AS execution_outcome,
                       e.action AS execution_action,
                       e.symbol AS execution_symbol
                FROM risk_reservations r
                LEFT JOIN decisions d
                  ON d.account_id = r.account_id
                 AND d.request_id = r.request_id
                LEFT JOIN executions e
                  ON e.account_id = r.account_id
                 AND e.request_id = r.request_id
                ORDER BY r.account_id, r.request_id
                """
            ).fetchall()
            execution_rows = connection.execute(
                """
                SELECT e.account_id, e.request_id, e.action, e.symbol,
                       e.outcome,
                       d.action AS decision_action,
                       d.symbol AS decision_symbol,
                       r.status AS reservation_status,
                       e.request_hash AS execution_request_hash
                FROM executions e
                LEFT JOIN decisions d
                  ON d.account_id = e.account_id
                 AND d.request_id = e.request_id
                LEFT JOIN risk_reservations r
                  ON r.account_id = e.account_id
                 AND r.request_id = e.request_id
                ORDER BY e.account_id, e.request_id
                """
            ).fetchall()
            decision_rows = connection.execute(
                """
                SELECT d.account_id, d.request_id, d.action, d.symbol,
                       d.allowed, d.reservation_risk,
                       e.outcome AS execution_outcome,
                       r.status AS reservation_status,
                       r.reserved_risk AS linked_reservation_risk
                FROM decisions d
                LEFT JOIN executions e
                  ON e.account_id = d.account_id
                 AND e.request_id = d.request_id
                LEFT JOIN risk_reservations r
                  ON r.account_id = d.account_id
                 AND r.request_id = d.request_id
                ORDER BY d.account_id, d.request_id
                """
            ).fetchall()
            credential_rows = connection.execute(
                """
                SELECT c.credential_id, c.account_id
                FROM account_credentials c
                LEFT JOIN accounts a ON a.account_id = c.account_id
                WHERE a.account_id IS NULL
                ORDER BY c.credential_id
                """
            ).fetchall()
            account_rows = connection.execute(
                """
                SELECT account_id, reserved_open_risk
                FROM accounts
                ORDER BY account_id
                """
            ).fetchall()

        issues: list[str] = []
        active_totals: dict[str, Decimal] = {}

        def add(message: str) -> None:
            if len(issues) < limit:
                issues.append(message)

        for row in reservation_rows:
            account_id = str(row["account_id"])
            request_id = str(row["request_id"])
            status = str(row["status"])
            if status in ACTIVE_RESERVATION_STATUSES:
                try:
                    risk = Decimal(row["reserved_risk"])
                except (TypeError, ValueError, ArithmeticError):
                    risk = None
                if risk is None or not risk.is_finite() or risk <= ZERO:
                    add(
                        f"reservation {account_id}/{request_id} has invalid "
                        "reserved risk"
                    )
                else:
                    active_totals[account_id] = (
                        active_totals.get(account_id, ZERO) + risk
                    )
            if status not in ACTIVE_RESERVATION_STATUSES:
                add(f"reservation {account_id}/{request_id} has invalid status")
            if str(row["action"]) not in {"open", "modify"}:
                add(f"reservation {account_id}/{request_id} has invalid action")
            if not str(row["symbol"]).strip():
                add(f"reservation {account_id}/{request_id} has an empty symbol")
            if row["decision_hash"] is None:
                add(
                    f"reservation {account_id}/{request_id} has no decision"
                )
            else:
                if (
                    row["request_hash"] != row["decision_hash"]
                    or row["action"] != row["decision_action"]
                    or row["symbol"] != row["decision_symbol"]
                    or not bool(row["decision_allowed"])
                ):
                    add(
                        f"reservation {account_id}/{request_id} does not "
                        "match its allowed decision"
                    )
            if row["execution_outcome"] is None:
                if status in {"unknown", "committed"}:
                    add(
                        f"{status} reservation {account_id}/{request_id} "
                        "has no execution record"
                    )
            else:
                if (
                    row["execution_action"] != row["action"]
                    or row["execution_symbol"] != row["symbol"]
                ):
                    add(
                        f"reservation {account_id}/{request_id} does not "
                        "match its execution identity"
                    )
                if status == "pending":
                    add(
                        f"pending reservation {account_id}/{request_id} "
                        "already has an execution record"
                    )
                elif status == "unknown" and row["execution_outcome"] != "unknown":
                    add(
                        f"unknown reservation {account_id}/{request_id} "
                        "has a non-unknown execution outcome"
                    )
                elif status == "committed" and (
                    row["execution_outcome"] != "success"
                    or row["execution_at"] is None
                ):
                    add(
                        f"committed reservation {account_id}/{request_id} "
                        "is missing a successful execution"
                    )

            if status in {"pending", "unknown"} and row["execution_at"] is not None:
                add(
                    f"{status} reservation {account_id}/{request_id} has an "
                    "unexpected execution timestamp"
                )

        for row in execution_rows:
            account_id = str(row["account_id"])
            request_id = str(row["request_id"])
            outcome = str(row["outcome"])
            if outcome not in {"success", "failure", "unknown"}:
                add(f"execution {account_id}/{request_id} has invalid outcome")
            if row["decision_action"] is None:
                add(f"execution {account_id}/{request_id} has no decision")
            elif (
                row["action"] != row["decision_action"]
                or row["symbol"] != row["decision_symbol"]
            ):
                add(
                    f"execution {account_id}/{request_id} does not match "
                    "its decision"
                )
            if (
                not isinstance(row["execution_request_hash"], str)
                or not row["execution_request_hash"].strip()
            ):
                add(
                    f"execution {account_id}/{request_id} has an empty "
                    "request hash"
                )
            if (
                outcome == "unknown"
                and row["action"] in {"open", "modify"}
                and row["reservation_status"] is None
            ):
                add(
                    f"unknown execution {account_id}/{request_id} has no "
                    "risk reservation"
                )

        for row in decision_rows:
            account_id = str(row["account_id"])
            request_id = str(row["request_id"])
            action = str(row["action"])
            symbol = str(row["symbol"])
            if action not in {"open", "close", "modify", "cancel"}:
                add(f"decision {account_id}/{request_id} has invalid action")
            if not symbol.strip():
                add(f"decision {account_id}/{request_id} has an empty symbol")
            try:
                decision_risk = Decimal(row["reservation_risk"])
            except (TypeError, ValueError, ArithmeticError):
                decision_risk = None
            if (
                decision_risk is None
                or not decision_risk.is_finite()
                or decision_risk < ZERO
            ):
                add(
                    f"decision {account_id}/{request_id} has invalid "
                    "reservation risk"
                )
                decision_risk = ZERO
            try:
                allowed = int(row["allowed"])
            except (TypeError, ValueError):
                allowed = -1
            if allowed not in {0, 1}:
                add(f"decision {account_id}/{request_id} has invalid allowed flag")

            if allowed == 1 and action in {"open", "modify"}:
                execution_outcome = row["execution_outcome"]
                reservation_status = row["reservation_status"]
                if action == "open" and (
                    reservation_status is None
                    and execution_outcome not in {"success", "failure"}
                ):
                    add(
                        f"allowed open decision {account_id}/{request_id} "
                        "has no execution reservation"
                    )
                elif decision_risk > ZERO and reservation_status is None and (
                    execution_outcome not in {"success", "failure"}
                ):
                    add(
                        f"risk-increasing decision {account_id}/{request_id} "
                        "has no execution reservation"
                    )
                if reservation_status is not None:
                    try:
                        linked_risk = Decimal(row["linked_reservation_risk"])
                    except (TypeError, ValueError, ArithmeticError):
                        linked_risk = None
                    if (
                        linked_risk is None
                        or not linked_risk.is_finite()
                        or linked_risk != decision_risk
                    ):
                        add(
                            f"decision {account_id}/{request_id} does not "
                            "match its reservation risk"
                        )
            elif decision_risk > ZERO and action not in {"open", "modify"}:
                add(
                    f"decision {account_id}/{request_id} reserves risk for a "
                    "non-risk-increasing action"
                )

        for row in credential_rows:
            add(
                f"credential {row['credential_id']} references missing "
                f"account {row['account_id']}"
            )

        for row in account_rows:
            account_id = str(row["account_id"])
            try:
                stored_risk = Decimal(row["reserved_open_risk"])
            except (TypeError, ValueError, ArithmeticError):
                stored_risk = None
            expected_risk = active_totals.get(account_id, ZERO)
            if (
                stored_risk is None
                or not stored_risk.is_finite()
                or stored_risk < ZERO
                or stored_risk != expected_risk
            ):
                add(
                    f"account {account_id} reserved_open_risk cache does "
                    "not match active reservations"
                )

        return issues

    def risk_reservation_metrics(
        self,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            if account_id is None:
                rows = connection.execute(
                    "SELECT status, reserved_risk FROM risk_reservations"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT status, reserved_risk
                    FROM risk_reservations
                    WHERE account_id = ?
                    """,
                    (account_id,),
                ).fetchall()
        counts = {"pending": 0, "unknown": 0, "committed": 0}
        total = ZERO
        for row in rows:
            status = str(row["status"])
            if status not in counts:
                raise ValueError("persisted reservation status is invalid")
            value = Decimal(row["reserved_risk"])
            if not value.is_finite() or value < ZERO:
                raise ValueError("persisted reservation risk is invalid")
            counts[status] += 1
            total += value
        return {
            "pending": counts["pending"],
            "unknown": counts["unknown"],
            "committed": counts["committed"],
            "unresolved": counts["pending"] + counts["unknown"],
            "reserved_risk": total,
        }

    def list_risk_reservations(
        self,
        account_id: str | None = None,
        *,
        unresolved_only: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            clauses: list[str] = []
            values: list[str] = []
            if account_id is not None:
                clauses.append("account_id = ?")
                values.append(account_id)
            if unresolved_only:
                placeholders = ",".join("?" for _ in UNRESOLVED_RESERVATION_STATUSES)
                clauses.append(f"status IN ({placeholders})")
                values.extend(UNRESOLVED_RESERVATION_STATUSES)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                """
                SELECT account_id, request_id, request_hash, action, symbol,
                       reserved_risk, status, created_at, updated_at,
                       execution_at
                FROM risk_reservations
                """
                + where
                + " ORDER BY updated_at, account_id, request_id",
                values,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            risk = Decimal(row["reserved_risk"])
            if not risk.is_finite() or risk < ZERO:
                raise ValueError("persisted reservation risk is invalid")
            result.append(
                {
                    "account_id": row["account_id"],
                    "request_id": row["request_id"],
                    "request_hash": row["request_hash"],
                    "action": row["action"],
                    "symbol": row["symbol"],
                    "reserved_risk": str(risk),
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "execution_at": row["execution_at"],
                }
            )
        return result

    def record_backup_event(
        self,
        *,
        operation: str,
        success: bool,
        detail: str = "",
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO backup_runs (
                    operation, success, created_at, detail
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    operation,
                    int(success),
                    datetime.now(timezone.utc).isoformat(),
                    detail[:500],
                ),
            )
            connection.commit()

    def backup_metrics(self) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT operation, success, COUNT(*) AS count
                FROM backup_runs
                GROUP BY operation, success
                """
            ).fetchall()
            latest_success = connection.execute(
                """
                SELECT created_at, success
                FROM backup_runs
                WHERE operation = 'backup' AND success = 1
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            latest_attempt = connection.execute(
                """
                SELECT created_at, success
                FROM backup_runs
                WHERE operation = 'backup'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        counts: dict[str, Any] = {
            "backup_success": 0,
            "backup_failure": 0,
            "restore_success": 0,
            "restore_failure": 0,
        }
        for row in rows:
            key = (
                f"{row['operation']}_"
                f"{'success' if row['success'] else 'failure'}"
            )
            if key in counts:
                counts[key] = int(row["count"])
        counts["last_backup_at"] = (
            latest_success["created_at"]
            if latest_success is not None
            else None
        )
        counts["last_backup_success"] = (
            bool(latest_attempt["success"])
            if latest_attempt is not None
            else None
        )
        counts["last_backup_attempt_at"] = (
            latest_attempt["created_at"]
            if latest_attempt is not None
            else None
        )
        return counts

    def backup_to(self, output_path: str | Path) -> Path:
        if self.path == ":memory:":
            raise ValueError("in-memory state cannot be backed up by path")
        destination = self._absolute_path(output_path)
        source = self._absolute_path(self.path)
        self._assert_no_symlink_components(source, "state database")
        self._assert_no_symlink_components(destination, "backup destination")
        self._assert_no_symlink_components(
            destination.parent,
            "backup destination directory",
        )
        self._assert_regular_or_missing(source, "state database")
        if not source.is_file():
            raise FileNotFoundError(source)
        if not self.database_healthy():
            raise ValueError(
                "state database is not healthy; refusing to create a backup"
            )
        if destination == source:
            raise ValueError("backup destination cannot equal state database")
        self._assert_regular_or_missing(destination, "backup destination")
        for suffix in ("-wal", "-shm"):
            self._assert_regular_or_missing(
                destination.with_name(destination.name + suffix),
                f"backup destination {suffix} sidecar",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink_components(
            destination,
            "backup destination",
        )
        self._assert_directory(
            destination.parent,
            "backup destination directory",
        )
        temp_path: Path | None = None
        replaced = False
        displaced_sidecars: list[tuple[Path, Path]] = []
        try:
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=str(destination.parent),
            )
            temp_path = Path(temp_name)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                else:
                    os.chmod(temp_path, 0o600)
            finally:
                os.close(descriptor)
            with self._lock, self._connection() as source_connection:
                backup_connection = sqlite3.connect(str(temp_path))
                try:
                    source_connection.backup(backup_connection)
                    backup_connection.execute("PRAGMA wal_checkpoint(FULL)")
                    backup_connection.commit()
                finally:
                    backup_connection.close()
            self._checkpoint_file(temp_path)
            os.chmod(temp_path, 0o600)
            with closing(sqlite3.connect(str(temp_path))) as verification:
                verification.execute("PRAGMA foreign_keys=ON")
                check = verification.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise ValueError("backup quick_check failed")
                if verification.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall():
                    raise ValueError("backup foreign key check failed")
            self._fsync_file(temp_path)
            self._assert_regular_or_missing(
                destination,
                "backup destination",
            )
            displaced_sidecars = self._displace_destination_sidecars(
                destination
            )
            os.replace(temp_path, destination)
            replaced = True
            self._discard_destination_sidecars(displaced_sidecars)
            self._fsync_directory(destination.parent)
        except Exception as exc:
            if not replaced and temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if not replaced:
                self._restore_destination_sidecars(displaced_sidecars)
            else:
                self._discard_destination_sidecars(displaced_sidecars)
            if not replaced:
                try:
                    self.record_backup_event(
                        operation="backup",
                        success=False,
                        detail=str(exc),
                    )
                except Exception:
                    pass
            raise
        try:
            self.record_backup_event(
                operation="backup",
                success=True,
                detail=str(destination),
            )
        except Exception:
            # The backup artifact is already atomically installed. Telemetry
            # failure must not make a successful backup look like a failed one.
            pass
        return destination.resolve()

    def sync_account(
        self,
        *,
        account_id: str,
        account_type: AccountType,
        phase: AccountPhase,
        style: AccountStyle,
        initial_capital: Decimal,
        balance: Decimal,
        equity: Decimal,
        current_open_risk: Decimal,
        open_positions_count: int | None = None,
        pending_orders_count: int | None = None,
        as_of: datetime,
        received_at: datetime | None = None,
        profile: RuleProfile | None = None,
        bootstrap_day_start_balance: Decimal | None = None,
        bootstrap_highest_settled_balance: Decimal | None = None,
    ) -> StoredAccount:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        received_at = received_at or datetime.now(timezone.utc)
        if received_at.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        for decimal_value, field_name in (
            (initial_capital, "initial_capital"),
            (balance, "balance"),
            (equity, "equity"),
            (current_open_risk, "current_open_risk"),
        ):
            if (
                not isinstance(decimal_value, Decimal)
                or not decimal_value.is_finite()
            ):
                raise ValueError(f"{field_name} must be a finite decimal")
        if current_open_risk < 0:
            raise ValueError("current_open_risk cannot be negative")
        for inventory_value, field_name in (
            (open_positions_count, "open_positions_count"),
            (pending_orders_count, "pending_orders_count"),
        ):
            if inventory_value is not None and (
                isinstance(inventory_value, bool)
                or not isinstance(inventory_value, int)
                or inventory_value < 0
            ):
                raise ValueError(f"{field_name} must be non-negative")
        if account_type == AccountType.ONE_STEP and style == AccountStyle.SWING:
            raise ValueError("Swing style is only available for 2-Step accounts")
        if (
            account_type == AccountType.ONE_STEP
            and phase == AccountPhase.VERIFICATION
        ):
            raise ValueError("Verification is only available for 2-Step accounts")
        if profile is not None and (
            profile.account_type != account_type
            or profile.phase != phase
            or profile.style != style
        ):
            raise ValueError("profile does not match the synchronized account")
        current_day = ftmo_day_key(received_at, self.day_timezone)
        updated_at = received_at.astimezone(timezone.utc)
        as_of_utc = as_of.astimezone(timezone.utc)

        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()

            if row is None:
                if (
                    bootstrap_day_start_balance is None
                    or bootstrap_highest_settled_balance is None
                ):
                    raise ValueError(
                        "first account sync requires day_start_balance and "
                        "highest_settled_balance"
                    )
                day_start_balance = bootstrap_day_start_balance
                highest_settled_balance = bootstrap_highest_settled_balance
                if (
                    not isinstance(day_start_balance, Decimal)
                    or not day_start_balance.is_finite()
                    or not isinstance(highest_settled_balance, Decimal)
                    or not highest_settled_balance.is_finite()
                ):
                    raise ValueError(
                        "bootstrap balances must be finite decimals"
                    )
                data_uncertain = False
                day_locked = False
                breach_latched = False
                if day_start_balance <= 0:
                    raise ValueError("day_start_balance must be positive")
                if highest_settled_balance < initial_capital:
                    raise ValueError(
                        "highest_settled_balance cannot be below initial_capital"
                    )
                if highest_settled_balance < day_start_balance:
                    raise ValueError(
                        "highest_settled_balance cannot be below "
                        "day_start_balance"
                    )
            else:
                if row["account_type"] != account_type.value:
                    raise ValueError("account_type cannot change after bootstrap")
                if Decimal(row["initial_capital"]) != initial_capital:
                    raise ValueError(
                        "initial_capital cannot change after bootstrap"
                    )
                if row["style"] != style.value:
                    raise ValueError("style cannot change after bootstrap")
                stored_as_of = datetime.fromisoformat(row["as_of"])
                stored_as_of_utc = stored_as_of.astimezone(timezone.utc)
                if (
                    as_of_utc < stored_as_of_utc
                ):
                    raise ValueError(
                        "account sync timestamp cannot move backwards"
                    )
                if as_of_utc == stored_as_of_utc:
                    stored_inventory = (
                        row["open_positions_count"],
                        row["pending_orders_count"],
                    )
                    incoming_inventory = (
                        open_positions_count,
                        pending_orders_count,
                    )
                    if (
                        Decimal(row["balance"]) != balance
                        or Decimal(row["equity"]) != equity
                        or Decimal(row["current_open_risk"])
                        != current_open_risk
                        or stored_inventory != incoming_inventory
                    ):
                        raise ValueError(
                            "account sync timestamp already has different "
                            "snapshot content"
                        )
                phase_order = {
                    AccountPhase.EVALUATION.value: 0,
                    AccountPhase.VERIFICATION.value: 1,
                    AccountPhase.FTMO_ACCOUNT.value: 2,
                }
                if phase_order[phase.value] < phase_order[row["phase"]]:
                    raise ValueError("account phase cannot move backwards")
                if (
                    account_type == AccountType.TWO_STEP
                    and phase_order[phase.value] - phase_order[row["phase"]]
                    > 1
                ):
                    raise ValueError(
                        "two-step account phase cannot skip verification"
                    )
                day_start_balance = Decimal(row["day_start_balance"])
                highest_settled_balance = Decimal(
                    row["highest_settled_balance"]
                )
                data_uncertain = bool(row["data_uncertain"])
                day_locked = bool(row["day_locked"])
                breach_latched = bool(row["breach_latched"])

                settlement = connection.execute(
                    """
                    SELECT settled_balance, confirmed
                    FROM daily_settlements
                    WHERE account_id = ? AND ftmo_day = ?
                    """,
                    (account_id, current_day),
                ).fetchone()
                if row["ftmo_day"] != current_day:
                    if settlement is not None and settlement["confirmed"]:
                        settled_balance = Decimal(
                            settlement["settled_balance"]
                        )
                        data_uncertain = False
                    else:
                        settled_balance = Decimal(row["balance"])
                        data_uncertain = True
                    day_start_balance = settled_balance
                    day_locked = False
                    if account_type == AccountType.ONE_STEP:
                        highest_settled_balance = max(
                            highest_settled_balance,
                            settled_balance,
                        )
                    connection.execute(
                        """
                        INSERT INTO daily_settlements (
                            account_id, ftmo_day, settled_balance,
                            settled_at, source, confirmed
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(account_id, ftmo_day) DO UPDATE SET
                            settled_balance = excluded.settled_balance,
                            settled_at = excluded.settled_at,
                            source = excluded.source,
                            confirmed = excluded.confirmed
                        """,
                        (
                            account_id,
                            current_day,
                            str(settled_balance),
                            received_at.isoformat(),
                            (
                                "confirmed_schedule"
                                if not data_uncertain
                                else "inferred_last_sync"
                            ),
                            0 if data_uncertain else 1,
                        ),
                    )
                elif settlement is not None and settlement["confirmed"]:
                    day_start_balance = Decimal(
                        settlement["settled_balance"]
                    )
                    if account_type == AccountType.ONE_STEP:
                        highest_settled_balance = max(
                            highest_settled_balance,
                            day_start_balance,
                        )
                    data_uncertain = False

            if profile is not None:
                observed_snapshot = AccountSnapshot(
                    initial_capital=initial_capital,
                    day_start_balance=day_start_balance,
                    highest_settled_balance=highest_settled_balance,
                    balance=balance,
                    equity=equity,
                    current_open_risk=current_open_risk,
                    as_of=as_of,
                    data_uncertain=data_uncertain,
                    open_positions_count=open_positions_count,
                    pending_orders_count=pending_orders_count,
                )
                observed_status = RiskEngine(
                    profile,
                    day_timezone=self.day_timezone,
                ).status(observed_snapshot)
                if observed_status == "BREACH":
                    breach_latched = True
                elif observed_status == "LOCKED":
                    day_locked = True

            reserved_open_risk = ZERO
            if row is not None:
                # Do this only after the incoming snapshot has passed all
                # ordering and phase checks.  A stale rejected sync must not
                # consume a valid execution reservation.
                self._release_committed_reservations_connection(
                    connection,
                    account_id,
                    as_of_utc,
                )
                reserved_open_risk = self._active_reservation_risk_connection(
                    connection,
                    account_id,
                )

            connection.execute(
                """
                INSERT INTO accounts (
                    account_id, account_type, phase, style, initial_capital,
                    ftmo_day, day_start_balance, highest_settled_balance,
                    balance, equity, current_open_risk, reserved_open_risk,
                    open_positions_count,
                    pending_orders_count,
                    as_of, updated_at,
                    data_uncertain, day_locked, breach_latched
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    phase = excluded.phase,
                    style = excluded.style,
                    ftmo_day = excluded.ftmo_day,
                    day_start_balance = excluded.day_start_balance,
                    highest_settled_balance = excluded.highest_settled_balance,
                    balance = excluded.balance,
                    equity = excluded.equity,
                    current_open_risk = excluded.current_open_risk,
                    reserved_open_risk = excluded.reserved_open_risk,
                    open_positions_count = excluded.open_positions_count,
                    pending_orders_count = excluded.pending_orders_count,
                    as_of = excluded.as_of,
                    updated_at = excluded.updated_at,
                    data_uncertain = excluded.data_uncertain,
                    day_locked = excluded.day_locked,
                    breach_latched = excluded.breach_latched
                """,
                (
                    account_id,
                    account_type.value,
                    phase.value,
                    style.value,
                    str(initial_capital),
                    current_day,
                    str(day_start_balance),
                    str(highest_settled_balance),
                    str(balance),
                    str(equity),
                    str(current_open_risk),
                    str(reserved_open_risk),
                    open_positions_count,
                    pending_orders_count,
                    as_of.isoformat(),
                    updated_at.isoformat(),
                    int(data_uncertain),
                    int(day_locked),
                    int(breach_latched),
                ),
            )
            connection.commit()

        return self.get_account(account_id)

    def _stored_account_from_row(
        self,
        row: sqlite3.Row,
        now: datetime | None = None,
        reserved_open_risk: Decimal | None = None,
    ) -> StoredAccount:
        now = now or datetime.now(timezone.utc)
        as_of = datetime.fromisoformat(row["as_of"])
        age = max(
            0,
            int(
                (now - as_of.astimezone(timezone.utc)).total_seconds()
            ),
        )
        if reserved_open_risk is None:
            reserved_open_risk = Decimal(
                row["reserved_open_risk"]
                if "reserved_open_risk" in row.keys()
                else "0"
            )
        if not reserved_open_risk.is_finite() or reserved_open_risk < ZERO:
            raise ValueError("persisted reserved open risk is invalid")
        return StoredAccount(
            account_id=row["account_id"],
            account_type=AccountType(row["account_type"]),
            phase=AccountPhase(row["phase"]),
            style=AccountStyle(row["style"]),
            ftmo_day=row["ftmo_day"],
            snapshot=AccountSnapshot(
                initial_capital=Decimal(row["initial_capital"]),
                day_start_balance=Decimal(row["day_start_balance"]),
                highest_settled_balance=Decimal(
                    row["highest_settled_balance"]
                ),
                balance=Decimal(row["balance"]),
                equity=Decimal(row["equity"]),
                current_open_risk=Decimal(row["current_open_risk"]),
                reserved_open_risk=reserved_open_risk,
                as_of=as_of,
                data_age_seconds=age,
                data_uncertain=bool(row["data_uncertain"]),
                day_locked=bool(row["day_locked"]),
                breach_latched=bool(row["breach_latched"]),
                open_positions_count=(
                    int(row["open_positions_count"])
                    if row["open_positions_count"] is not None
                    else None
                ),
                pending_orders_count=(
                    int(row["pending_orders_count"])
                    if row["pending_orders_count"] is not None
                    else None
                ),
            ),
        )

    def get_account(self, account_id: str) -> StoredAccount:
        now = datetime.now(timezone.utc)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, reserved_open_risk = self._load_account_connection(
                connection,
                account_id,
                now,
            )
            connection.commit()
        if row is None:
            raise KeyError(f"unknown account_id: {account_id}")
        return self._stored_account_from_row(
            row,
            now=now,
            reserved_open_risk=reserved_open_risk,
        )

    def _frequency_from_connection(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        now: datetime,
    ) -> FrequencyState:
        cutoff = (
            now.astimezone(timezone.utc) - timedelta(days=2)
        ).isoformat()
        rows = connection.execute(
            """
            SELECT kind, symbol, occurred_at
            FROM activity
            WHERE account_id = ? AND occurred_at >= ?
            ORDER BY occurred_at
            """,
            (account_id, cutoff),
        ).fetchall()
        open_times: list[datetime] = []
        request_times: list[datetime] = []
        last_modify_by_symbol: dict[str, datetime] = {}
        for row in rows:
            occurred_at = datetime.fromisoformat(row["occurred_at"])
            if row["kind"] == "request":
                request_times.append(occurred_at)
            elif row["kind"] == "open":
                open_times.append(occurred_at)
            elif row["kind"] == "modify":
                last_modify_by_symbol[row["symbol"]] = occurred_at
        return FrequencyState(
            open_times=open_times,
            request_times=request_times,
            last_modify_by_symbol=last_modify_by_symbol,
        )

    def _insert_activity_connection(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        kind: str,
        symbol: str,
        occurred_at: datetime,
        request_id: str,
        detail: str,
    ) -> None:
        if kind not in {"request", "open", "modify", "execution"}:
            raise ValueError("unsupported activity kind")
        if occurred_at.tzinfo is None:
            raise ValueError("activity timestamp must be timezone-aware")
        occurred_at_utc = occurred_at.astimezone(timezone.utc)
        connection.execute(
            """
            INSERT OR IGNORE INTO activity (
                account_id, kind, symbol, occurred_at, request_id, detail
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                kind,
                symbol.upper(),
                occurred_at_utc.isoformat(),
                request_id,
                detail,
            ),
        )
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=3)
        ).isoformat()
        connection.execute(
            "DELETE FROM activity WHERE account_id = ? AND occurred_at < ?",
            (account_id, cutoff),
        )

    def evaluate_and_reserve(
        self,
        *,
        account_id: str,
        request_id: str,
        request_hash: str,
        action: str,
        symbol: str,
        occurred_at: datetime,
        evaluator: Callable[[StoredAccount, FrequencyState], Mapping[str, Any]],
        block_on_unknown_execution: bool = False,
        reservation_risk: Decimal = ZERO,
    ) -> dict[str, Any]:
        """Evaluate and reserve frequency state atomically for one request."""

        if (
            not isinstance(reservation_risk, Decimal)
            or not reservation_risk.is_finite()
            or reservation_risk < ZERO
        ):
            raise ValueError("reservation_risk must be a finite non-negative decimal")
        now = datetime.now(timezone.utc)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._promote_expired_pending_reservations_connection(
                connection,
                account_id,
                now,
            )
            existing = connection.execute(
                """
                SELECT request_hash, response_json, allowed, created_at
                FROM decisions
                WHERE account_id = ? AND request_id = ?
                """,
                (account_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ValueError(
                        "request_id has already been used with different content"
                    )
                execution = connection.execute(
                    """
                    SELECT outcome
                    FROM executions
                    WHERE account_id = ? AND request_id = ?
                    """,
                    (account_id, request_id),
                ).fetchone()
                if execution is not None:
                    outcome = str(execution["outcome"])
                    if outcome == "unknown":
                        response = self._blocked_replay_response(
                            existing["response_json"],
                            code="REJECT_UNKNOWN_EXECUTION",
                            reason=(
                                "an execution outcome is unresolved; reconcile "
                                "the platform order before retrying"
                            ),
                            execution_outcome=outcome,
                        )
                    elif outcome in {"success", "failure"}:
                        response = self._blocked_replay_response(
                            existing["response_json"],
                            code="REJECT_REQUEST_REPLAY",
                            reason=(
                                "request_id has already been reconciled; use a "
                                "new request_id for another submission"
                            ),
                            execution_outcome=outcome,
                        )
                    else:
                        raise ValueError(
                            "persisted execution outcome is invalid"
                        )
                    connection.commit()
                    return response
                try:
                    response = json.loads(existing["response_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "persisted decision response is invalid"
                    ) from exc
                if not isinstance(response, dict):
                    raise ValueError(
                        "persisted decision response must be an object"
                    )
                if bool(existing["allowed"]):
                    response = self._blocked_replay_response(
                        existing["response_json"],
                        code="REJECT_REQUEST_REPLAY",
                        reason=(
                            "request_id already has an approval; reconcile the "
                            "platform execution result before retrying"
                        ),
                    )
                connection.commit()
                return response

            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown account_id: {account_id}")
            reserved_open_risk = self._refresh_reserved_risk_connection(
                connection,
                account_id,
            )
            account = self._stored_account_from_row(row, now=now)
            account = replace(
                account,
                snapshot=replace(
                    account.snapshot,
                    reserved_open_risk=reserved_open_risk,
                ),
            )
            if account.ftmo_day != ftmo_day_key(
                occurred_at,
                self.day_timezone,
            ):
                account = replace(
                    account,
                    snapshot=replace(
                        account.snapshot,
                        data_uncertain=True,
                    ),
                )
            frequency = self._frequency_from_connection(
                connection,
                account_id,
                occurred_at,
            )
            response = dict(evaluator(account, frequency))
            unknown_count = 0
            if block_on_unknown_execution:
                unknown_row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM (
                        SELECT request_id
                        FROM executions
                        WHERE account_id = ? AND outcome = 'unknown'
                        UNION
                        SELECT request_id
                        FROM risk_reservations
                        WHERE account_id = ? AND status = 'unknown'
                    )
                    """,
                    (account_id, account_id),
                ).fetchone()
                unknown_count = (
                    int(unknown_row["count"])
                    if unknown_row is not None
                    else 0
                )
                decision = response.get("decision")
                if unknown_count > 0 and isinstance(decision, dict):
                    decision["code"] = "REJECT_UNKNOWN_EXECUTION"
                    decision["allowed"] = False
                    decision["reasons"] = [
                        "an execution outcome is unresolved; reconcile "
                        "the platform order before adding risk",
                    ]
                    response["unknown_execution_count"] = unknown_count
            allowed = bool(response.get("decision", {}).get("allowed"))
            if allowed and reservation_risk > ZERO:
                connection.execute(
                    """
                    INSERT INTO risk_reservations (
                        account_id, request_id, request_hash, action, symbol,
                        reserved_risk, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        account_id,
                        request_id,
                        request_hash,
                        action,
                        symbol.upper(),
                        str(reservation_risk),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                reserved_total = self._refresh_reserved_risk_connection(
                    connection,
                    account_id,
                )
                response["risk_reservation"] = {
                    "status": "pending",
                    "reserved_risk": str(reservation_risk),
                }
                account_payload = response.get("account")
                if isinstance(account_payload, dict):
                    current_open_risk = account_payload.get(
                        "current_open_risk",
                        "0",
                    )
                    account_payload["reserved_open_risk"] = str(
                        reserved_total
                    )
                    account_payload["effective_open_risk"] = str(
                        Decimal(str(current_open_risk)) + reserved_total
                    )
            response_json = json.dumps(
                response,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO decisions (
                    account_id, request_id, request_hash, action, symbol,
                    allowed, reservation_risk, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    request_id,
                    request_hash,
                    action,
                    symbol.upper(),
                    int(allowed),
                    str(reservation_risk),
                    response_json,
                    now.isoformat(),
                ),
            )
            self._insert_activity_connection(
                connection,
                account_id=account_id,
                kind="request",
                symbol=symbol,
                occurred_at=occurred_at,
                request_id=request_id,
                detail=str(response.get("decision", {}).get("code", "")),
            )
            if allowed:
                if action == "open":
                    self._insert_activity_connection(
                        connection,
                        account_id=account_id,
                        kind="open",
                        symbol=symbol,
                        occurred_at=occurred_at,
                        request_id=request_id,
                        detail="risk reservation",
                    )
                elif action == "modify":
                    self._insert_activity_connection(
                        connection,
                        account_id=account_id,
                        kind="modify",
                        symbol=symbol,
                        occurred_at=occurred_at,
                        request_id=request_id,
                        detail="allowed modification",
                    )
            connection.commit()
            return response

    def record_execution(
        self,
        *,
        account_id: str,
        request_id: str,
        request_hash: str,
        action: str,
        symbol: str,
        occurred_at: datetime,
        outcome: str,
        detail: str,
    ) -> dict[str, Any]:
        """Record an execution result idempotently."""

        if outcome not in {"success", "failure", "unknown"}:
            raise ValueError("outcome must be success, failure, or unknown")
        normalized_symbol = symbol.upper()
        execution_at = datetime.now(timezone.utc)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_hash, response_json, action, symbol, outcome
                FROM executions
                WHERE account_id = ? AND request_id = ?
                """,
                (account_id, request_id),
            ).fetchone()
            resolving_unknown = False
            if existing is not None:
                if (
                    existing["action"] != action
                    or existing["symbol"] != normalized_symbol
                ):
                    raise ValueError(
                        "execution request_id has already been used for a "
                        "different action or symbol"
                    )
                if existing["outcome"] == "unknown" and outcome == "unknown":
                    # Unknown is sticky and non-releasing. A late report after
                    # lease reconciliation may carry different diagnostics,
                    # but it must return the existing locked result.
                    return json.loads(existing["response_json"])
                if existing["outcome"] == "unknown" and outcome in {
                    "success",
                    "failure",
                }:
                    resolving_unknown = True
                elif existing["request_hash"] != request_hash:
                    raise ValueError(
                        "execution request_id has already been used with "
                        "different content"
                    )
                else:
                    return json.loads(existing["response_json"])

            decision = connection.execute(
                """
                SELECT action, symbol, allowed
                FROM decisions
                WHERE account_id = ? AND request_id = ?
                """,
                (account_id, request_id),
            ).fetchone()
            if decision is None:
                raise ValueError(
                    "execution result does not reference a known evaluation"
                )
            if decision["action"] != action or decision["symbol"] != normalized_symbol:
                raise ValueError(
                    "execution result does not match the evaluated request"
                )
            if not decision["allowed"]:
                raise ValueError(
                    "execution result cannot be recorded for a rejected request"
                )

            reservation = connection.execute(
                """
                SELECT reserved_risk, status
                FROM risk_reservations
                WHERE account_id = ? AND request_id = ?
                """,
                (account_id, request_id),
            ).fetchone()
            reservation_kind = (
                action
                if outcome == "failure" and action in {"open", "modify"}
                else None
            )
            reservation_released = (
                reservation is not None and reservation_kind is not None
            )
            reservation_committed = (
                reservation is not None
                and outcome == "success"
                and action in {"open", "modify"}
            )
            if reservation_kind is not None:
                connection.execute(
                    """
                    DELETE FROM activity
                    WHERE account_id = ? AND kind = ? AND request_id = ?
                    """,
                    (account_id, reservation_kind, request_id),
                )
            if reservation_released:
                connection.execute(
                    "DELETE FROM risk_reservations "
                    "WHERE account_id = ? AND request_id = ?",
                    (account_id, request_id),
                )
            elif reservation_committed:
                connection.execute(
                    """
                    UPDATE risk_reservations
                    SET status = 'committed', execution_at = ?, updated_at = ?
                    WHERE account_id = ? AND request_id = ?
                    """,
                    (
                        execution_at.isoformat(),
                        execution_at.isoformat(),
                        account_id,
                        request_id,
                    ),
                )
            elif reservation is not None and outcome == "unknown":
                connection.execute(
                    """
                    UPDATE risk_reservations
                    SET status = 'unknown', updated_at = ?
                    WHERE account_id = ? AND request_id = ?
                    """,
                    (execution_at.isoformat(), account_id, request_id),
                )
            if reservation is not None:
                self._refresh_reserved_risk_connection(connection, account_id)
            response = {
                "ok": True,
                "account_id": account_id,
                "request_id": request_id,
                "execution_recorded": True,
                "outcome": outcome,
                "reservation_released": reservation_released,
                "reservation_committed": reservation_committed,
                "resolved_unknown": resolving_unknown,
            }
            response_json = json.dumps(
                response,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            if resolving_unknown:
                connection.execute(
                    """
                    UPDATE executions
                    SET request_hash = ?, outcome = ?, response_json = ?,
                        created_at = ?
                    WHERE account_id = ? AND request_id = ?
                    """,
                    (
                        request_hash,
                        outcome,
                        response_json,
                        execution_at.isoformat(),
                        account_id,
                        request_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE activity
                    SET detail = ?
                    WHERE account_id = ? AND kind = 'execution'
                      AND request_id = ?
                    """,
                    (detail, account_id, request_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO executions (
                        account_id, request_id, request_hash, action, symbol,
                        outcome, response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        request_id,
                        request_hash,
                        action,
                        normalized_symbol,
                        outcome,
                        response_json,
                        execution_at.isoformat(),
                    ),
                )
            self._insert_activity_connection(
                connection,
                account_id=account_id,
                kind="execution",
                symbol=symbol,
                occurred_at=occurred_at,
                request_id=request_id,
                detail=detail,
            )
            connection.commit()
            return response

    def record_activity(
        self,
        *,
        account_id: str,
        kind: str,
        symbol: str,
        occurred_at: datetime,
        request_id: str,
        detail: str = "",
    ) -> None:
        with self._lock, self._connection() as connection:
            self._insert_activity_connection(
                connection,
                account_id=account_id,
                kind=kind,
                symbol=symbol,
                occurred_at=occurred_at,
                request_id=request_id,
                detail=detail,
            )
            connection.commit()

    def release_activity(
        self,
        *,
        account_id: str,
        kind: str,
        request_id: str,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                DELETE FROM activity
                WHERE account_id = ? AND kind = ? AND request_id = ?
                """,
                (account_id, kind, request_id),
            )
            connection.commit()

    def frequency(self, account_id: str) -> FrequencyState:
        with self._lock, self._connection() as connection:
            return self._frequency_from_connection(
                connection,
                account_id,
                datetime.now(timezone.utc),
            )

    def confirm_settlement(
        self,
        *,
        account_id: str,
        ftmo_day: str,
        settled_balance: Decimal,
        settled_at: datetime,
        source: str,
    ) -> StoredAccount:
        if (
            not isinstance(settled_balance, Decimal)
            or not settled_balance.is_finite()
        ):
            raise ValueError("settled_balance must be a finite decimal")
        if settled_balance <= 0:
            raise ValueError("settled_balance must be positive")
        if settled_at.tzinfo is None:
            raise ValueError("settled_at must be timezone-aware")
        if not ftmo_day:
            raise ValueError("ftmo_day is required")
        if not source.strip():
            raise ValueError("source must be non-empty")
        if ftmo_day_key(settled_at, self.day_timezone) != ftmo_day:
            raise ValueError("ftmo_day does not match settled_at")

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown account_id: {account_id}")
            existing = connection.execute(
                """
                SELECT settled_balance, settled_at, source, confirmed
                FROM daily_settlements
                WHERE account_id = ? AND ftmo_day = ?
                """,
                (account_id, ftmo_day),
            ).fetchone()
            if existing is not None and existing["confirmed"]:
                existing_at = datetime.fromisoformat(existing["settled_at"])
                if settled_at < existing_at:
                    raise ValueError(
                        "confirmed settlement cannot be replaced by an older record"
                    )
                if (
                    settled_at == existing_at
                    and (
                        Decimal(existing["settled_balance"]) != settled_balance
                        or existing["source"] != source
                    )
                ):
                    raise ValueError(
                        "confirmed settlement timestamp already has different content"
                    )
            connection.execute(
                """
                INSERT INTO daily_settlements (
                    account_id, ftmo_day, settled_balance, settled_at,
                    source, confirmed
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(account_id, ftmo_day) DO UPDATE SET
                    settled_balance = excluded.settled_balance,
                    settled_at = excluded.settled_at,
                    source = excluded.source,
                    confirmed = 1
                """,
                (
                    account_id,
                    ftmo_day,
                    str(settled_balance),
                    settled_at.isoformat(),
                    source,
                ),
            )
            if row["ftmo_day"] == ftmo_day:
                highest = Decimal(row["highest_settled_balance"])
                if row["account_type"] == AccountType.ONE_STEP.value:
                    highest = max(highest, settled_balance)
                connection.execute(
                    """
                    UPDATE accounts
                    SET day_start_balance = ?,
                        highest_settled_balance = ?,
                        data_uncertain = 0,
                        updated_at = ?
                    WHERE account_id = ?
                    """,
                    (
                        str(settled_balance),
                        str(highest),
                        datetime.now(timezone.utc).isoformat(),
                        account_id,
                    ),
                )
            connection.commit()
        return self.get_account(account_id)
