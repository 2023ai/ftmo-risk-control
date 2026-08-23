"""Persistent account, settlement, idempotency, and frequency state."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

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


@dataclass(frozen=True)
class StoredAccount:
    account_id: str
    account_type: AccountType
    phase: AccountPhase
    style: AccountStyle
    snapshot: AccountSnapshot
    ftmo_day: str


class StateStore:
    def __init__(
        self,
        path: str | Path,
        day_timezone: str = "Europe/Prague",
    ):
        self.path = str(path)
        self.day_timezone = day_timezone
        self._lock = threading.RLock()
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
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        self._secure_database_sidecars()
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

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
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(account_id, request_id),
                    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
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
            connection.commit()

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
        if current_open_risk < 0:
            raise ValueError("current_open_risk cannot be negative")
        if account_type == AccountType.ONE_STEP and style == AccountStyle.SWING:
            raise ValueError("Swing style is only available for 2-Step accounts")
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
                data_uncertain = False
                day_locked = False
                breach_latched = False
                if day_start_balance <= 0:
                    raise ValueError("day_start_balance must be positive")
                if highest_settled_balance < initial_capital:
                    raise ValueError(
                        "highest_settled_balance cannot be below initial_capital"
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
                if (
                    as_of.astimezone(timezone.utc)
                    < stored_as_of.astimezone(timezone.utc)
                ):
                    raise ValueError(
                        "account sync timestamp cannot move backwards"
                    )
                if (
                    row["phase"] == AccountPhase.FTMO_ACCOUNT.value
                    and phase == AccountPhase.EVALUATION
                ):
                    raise ValueError(
                        "phase cannot move from ftmo_account back to evaluation"
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
                    balance, equity, current_open_risk, as_of, updated_at,
                    data_uncertain, day_locked, breach_latched
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    phase = excluded.phase,
                    style = excluded.style,
                    ftmo_day = excluded.ftmo_day,
                    day_start_balance = excluded.day_start_balance,
                    highest_settled_balance = excluded.highest_settled_balance,
                    balance = excluded.balance,
                    equity = excluded.equity,
                    current_open_risk = excluded.current_open_risk,
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
                SELECT request_hash, response_json
                FROM executions
                WHERE account_id = ? AND request_id = ?
                """,
                (account_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ValueError(
                        "execution request_id has already been used with "
                        "different content"
                    )
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
            }
            response_json = json.dumps(
                response,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO executions (
                    account_id, request_id, request_hash, response_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    request_id,
                    request_hash,
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
        if settled_balance <= 0:
            raise ValueError("settled_balance must be positive")
        if settled_at.tzinfo is None:
            raise ValueError("settled_at must be timezone-aware")
        if not ftmo_day:
            raise ValueError("ftmo_day is required")
        if not source.strip():
            raise ValueError("source must be non-empty")

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
