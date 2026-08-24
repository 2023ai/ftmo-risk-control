"""Persistent account, settlement, idempotency, and frequency state."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import sqlite3
import threading
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
        self.path = str(path)
        self.day_timezone = day_timezone
        self._lock = threading.RLock()
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
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
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
            if not os.path.exists(candidate):
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(candidate, flags)
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

                CREATE TABLE IF NOT EXISTS calendar_snapshots (
                    calendar_type TEXT PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
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
            execution_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(executions)"
                ).fetchall()
            }
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
            connection.commit()

    @staticmethod
    def _credential_digest(secret: str, salt: bytes) -> bytes:
        return hashlib.sha256(salt + secret.encode("utf-8")).digest()

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

    def save_calendar_snapshot(
        self,
        *,
        calendar_type: str,
        fetched_at: datetime,
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
        payload_json = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        content_hash = hashlib.sha256(
            payload_json.encode("utf-8")
        ).hexdigest()
        fetched_at_utc = fetched_at.astimezone(timezone.utc)
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
                    )
                ):
                    raise ValueError(
                        f"{calendar_type} calendar timestamp already has "
                        "different persisted content"
                    )
            connection.execute(
                """
                INSERT INTO calendar_snapshots (
                    calendar_type, fetched_at, content_hash, payload_json,
                    rule_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_type) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    content_hash = excluded.content_hash,
                    payload_json = excluded.payload_json,
                    rule_version = excluded.rule_version,
                    created_at = excluded.created_at
                """,
                (
                    calendar_type,
                    fetched_at_utc.isoformat(),
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
            result[row["calendar_type"]] = CalendarSnapshot(
                calendar_type=row["calendar_type"],
                fetched_at=datetime.fromisoformat(row["fetched_at"]),
                content_hash=row["content_hash"],
                payload=json.loads(row["payload_json"]),
                rule_version=row["rule_version"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        return result

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
        if not not_before.tzinfo or not expires_at.tzinfo:
            raise ValueError("credential timestamps must be timezone-aware")
        if expires_at <= not_before:
            raise ValueError("expires_at must be after not_before")
        now = now or datetime.now(timezone.utc)
        if not now.tzinfo:
            raise ValueError("now must be timezone-aware")
        credential_id = uuid4().hex
        secret = f"rsk_{credential_id}.{secrets.token_urlsafe(32)}"
        salt = secrets.token_bytes(16)
        digest = self._credential_digest(secret, salt)
        created_at = now.astimezone(timezone.utc)
        scopes_tuple = tuple(sorted(set(scopes)))
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
                    not_before.astimezone(timezone.utc).isoformat(),
                    expires_at.astimezone(timezone.utc).isoformat(),
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
        if not account_id or not secret or not scope:
            return None
        prefix, separator, _ = secret.partition(".")
        if separator != "." or not prefix.startswith("rsk_"):
            return None
        credential_id = prefix[4:]
        if len(credential_id) != 32:
            return None
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
        if overlap_seconds < 0:
            raise ValueError("overlap_seconds cannot be negative")
        now = now or datetime.now(timezone.utc)
        if not_before.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("credential timestamps must be timezone-aware")
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
                SELECT account_id, scopes_json
                FROM account_credentials
                WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown credential_id: {credential_id}")
            selected_scopes = tuple(
                scopes or tuple(json.loads(row["scopes_json"]))
            )
            if any(
                scope not in ACCOUNT_CREDENTIAL_SCOPES
                for scope in selected_scopes
            ):
                raise ValueError("credential scopes contain an unsupported scope")
            old_revoked_at = now + timedelta(seconds=overlap_seconds)
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
                    not_before.astimezone(timezone.utc).isoformat(),
                    expires_at.astimezone(timezone.utc).isoformat(),
                    json.dumps(
                        tuple(sorted(set(selected_scopes))),
                        ensure_ascii=True,
                    ),
                    now.astimezone(timezone.utc).isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE account_credentials
                SET revoked_at = ?
                WHERE credential_id = ?
                  AND (revoked_at IS NULL OR revoked_at > ?)
                """,
                (
                    old_revoked_at.astimezone(timezone.utc).isoformat(),
                    credential_id,
                    old_revoked_at.astimezone(timezone.utc).isoformat(),
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
        revoked_at = (revoked_at or datetime.now(timezone.utc)).astimezone(
            timezone.utc
        )
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE account_credentials
                SET revoked_at = ?
                WHERE credential_id = ?
                """,
                (revoked_at.isoformat(), credential_id),
            )
            row = connection.execute(
                "SELECT * FROM account_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise KeyError(f"unknown credential_id: {credential_id}")
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
        now_utc = (now or datetime.now(timezone.utc)).astimezone(
            timezone.utc
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
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM accounts ORDER BY account_id"
            ).fetchall()
        return [self._stored_account_from_row(row) for row in rows]

    def database_healthy(self) -> bool:
        if self.path != ":memory:" and not Path(self.path).is_file():
            return False
        try:
            with self._lock, self._connection() as connection:
                row = connection.execute("SELECT 1").fetchone()
                tables = {
                    item["name"]
                    for item in connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table'
                        """
                    ).fetchall()
                }
            required = {
                "accounts",
                "calendar_snapshots",
                "account_credentials",
                "activity",
                "daily_settlements",
                "decisions",
                "executions",
                "closed_trades",
                "qualification_history_status",
                "qualification_trading_days",
                "backup_runs",
            }
            return bool(row and row[0] == 1 and required <= tables)
        except (OSError, sqlite3.DatabaseError):
            return False

    def unknown_execution_count(self, account_id: str | None = None) -> int:
        with self._lock, self._connection() as connection:
            if account_id is None:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM executions
                    WHERE outcome = 'unknown'
                    """
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM executions
                    WHERE account_id = ? AND outcome = 'unknown'
                    """,
                    (account_id,),
                ).fetchone()
        return int(row["count"]) if row is not None else 0

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
        destination = Path(output_path).expanduser().resolve()
        source = Path(self.path).expanduser().resolve()
        if destination == source:
            raise ValueError("backup destination cannot equal state database")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_name(
            f".{destination.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            with self._lock, self._connection() as source_connection:
                backup_connection = sqlite3.connect(str(temp_path))
                try:
                    source_connection.backup(backup_connection)
                    backup_connection.execute("PRAGMA wal_checkpoint(FULL)")
                    backup_connection.commit()
                finally:
                    backup_connection.close()
            os.chmod(temp_path, 0o600)
            with closing(sqlite3.connect(str(temp_path))) as verification:
                check = verification.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise ValueError("backup quick_check failed")
            os.replace(temp_path, destination)
            os.chmod(destination, 0o600)
            self.record_backup_event(
                operation="backup",
                success=True,
                detail=str(destination),
            )
            return destination
        except Exception as exc:
            try:
                if temp_path.exists():
                    temp_path.unlink()
                self.record_backup_event(
                    operation="backup",
                    success=False,
                    detail=str(exc),
                )
            except Exception:
                pass
            raise

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
                as_of_utc = as_of.astimezone(timezone.utc)
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

            connection.execute(
                """
                INSERT INTO accounts (
                    account_id, account_type, phase, style, initial_capital,
                    ftmo_day, day_start_balance, highest_settled_balance,
                    balance, equity, current_open_risk, open_positions_count,
                    pending_orders_count,
                    as_of, updated_at,
                    data_uncertain, day_locked, breach_latched
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    phase = excluded.phase,
                    style = excluded.style,
                    ftmo_day = excluded.ftmo_day,
                    day_start_balance = excluded.day_start_balance,
                    highest_settled_balance = excluded.highest_settled_balance,
                    balance = excluded.balance,
                    equity = excluded.equity,
                    current_open_risk = excluded.current_open_risk,
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
    ) -> StoredAccount:
        now = now or datetime.now(timezone.utc)
        as_of = datetime.fromisoformat(row["as_of"])
        age = max(
            0,
            int(
                (now - as_of.astimezone(timezone.utc)).total_seconds()
            ),
        )
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
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown account_id: {account_id}")
        return self._stored_account_from_row(row)

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
    ) -> dict[str, Any]:
        """Evaluate and reserve frequency state atomically for one request."""

        now = datetime.now(timezone.utc)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_hash, response_json
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
                return json.loads(existing["response_json"])

            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown account_id: {account_id}")
            account = self._stored_account_from_row(row, now=now)
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
                    FROM executions
                    WHERE account_id = ? AND outcome = 'unknown'
                    """,
                    (account_id,),
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
                    allowed, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    request_id,
                    request_hash,
                    action,
                    symbol.upper(),
                    int(bool(response.get("decision", {}).get("allowed"))),
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
            if response.get("decision", {}).get("allowed"):
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
                    or existing["symbol"] != symbol.upper()
                ):
                    raise ValueError(
                        "execution request_id has already been used for a "
                        "different action or symbol"
                    )
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
            if decision["action"] != action or decision["symbol"] != symbol.upper():
                raise ValueError(
                    "execution result does not match the evaluated request"
                )
            if not decision["allowed"]:
                raise ValueError(
                    "execution result cannot be recorded for a rejected request"
                )

            reservation_kind = (
                action
                if outcome == "failure" and action in {"open", "modify"}
                else None
            )
            reservation_released = reservation_kind is not None
            if reservation_kind is not None:
                connection.execute(
                    """
                    DELETE FROM activity
                    WHERE account_id = ? AND kind = ? AND request_id = ?
                    """,
                    (account_id, reservation_kind, request_id),
                )
            response = {
                "ok": True,
                "account_id": account_id,
                "request_id": request_id,
                "execution_recorded": True,
                "outcome": outcome,
                "reservation_released": reservation_released,
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
                        datetime.now(timezone.utc).isoformat(),
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
                        symbol.upper(),
                        outcome,
                        response_json,
                        datetime.now(timezone.utc).isoformat(),
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
