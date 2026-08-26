"""Local HTTP adapter for the shared FTMO risk engine.

The service is intentionally stdlib-only so it can run next to MT5 or cTrader
without adding a Python web framework. Bind it to localhost unless a separate
authenticated gateway is explicitly deployed.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import ssl
import stat
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import EnumMeta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .risk_engine import (
    AccountPhase,
    AccountSnapshot,
    AccountStyle,
    AccountType,
    Action,
    Decision,
    DecisionCode,
    FrequencyState,
    MarketClosure,
    NewsEvent,
    RiskEngine,
    RuleProfile,
    TradeRequest,
    ftmo_day_key,
    validate_config,
)
from .qualification import qualification_snapshot
from .state_store import (
    ACCOUNT_CREDENTIAL_SCOPES,
    PLATFORM_CREDENTIAL_SCOPES,
    StateStore,
    StoredAccount,
)


LOGGER = logging.getLogger(__name__)
ConfigSource = str | Path | Mapping[str, Any]


class RequestError(ValueError):
    """A client supplied an invalid risk request."""


class EndpointDisabledError(RequestError):
    """An endpoint is intentionally disabled in the current server mode."""


class AuthorizationError(RequestError):
    """The caller is not authorized for the requested account or scope."""


class ForbiddenError(AuthorizationError):
    """The caller is authenticated but lacks the requested scope."""


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestError(
                "request body must not contain duplicate object keys"
            )
        result[key] = value
    return result


ACCOUNT_SCOPES = {
    "/v1/account-sync": "account:sync",
    "/v1/settlement-sync": "account:settlement",
    "/v1/evaluate": "trade:evaluate",
    "/v1/execution-result": "trade:execution",
    "/v1/news-status": "calendar:read",
    "/v1/market-status": "calendar:read",
    "/v1/closed-trade-sync": "qualification:write",
    "/v1/trading-day-sync": "qualification:write",
    "/v1/qualification-history-sync": "qualification:write",
}

METRIC_ENDPOINTS = frozenset(
    {
        "/dashboard/qualification",
        "/health",
        "/ready",
        "/metrics",
        "/v1/qualification",
        "/v1/qualification/accounts",
        "/v1/admin/credentials",
        *ACCOUNT_SCOPES,
        "/v1/news-sync",
        "/v1/market-sync",
        "/v1/position-size",
    }
)
METRIC_ENDPOINT_PATTERNS = (
    (
        re.compile(r"/v1/admin/accounts/[^/]+/credentials"),
        "/v1/admin/accounts/{account_id}/credentials",
    ),
    (
        re.compile(r"/v1/admin/credentials/[^/]+/rotate"),
        "/v1/admin/credentials/{credential_id}/rotate",
    ),
    (
        re.compile(r"/v1/admin/credentials/[^/]+/revoke"),
        "/v1/admin/credentials/{credential_id}/revoke",
    ),
)


def _path_only(path: str) -> str:
    return urlsplit(path).path


def _metric_endpoint(path: str) -> str:
    endpoint = _path_only(path)
    if endpoint in METRIC_ENDPOINTS:
        return endpoint
    for pattern, normalized in METRIC_ENDPOINT_PATTERNS:
        if pattern.fullmatch(endpoint):
            return normalized
    return "/__unknown__"


def _news_calendar_payload(
    values: list[NewsEvent],
) -> list[dict[str, Any]]:
    return [
        {
            "event_id": item.event_id,
            "release_time": item.release_time.isoformat(),
            "affected_symbols": sorted(item.affected_symbols),
            "importance": item.importance,
            "source": item.source,
        }
        for item in values
    ]


def _market_calendar_payload(
    values: list[MarketClosure],
) -> list[dict[str, Any]]:
    return [
        {
            "closure_id": item.closure_id,
            "start_time": item.start_time.isoformat(),
            "end_time": item.end_time.isoformat(),
            "affected_symbols": sorted(item.affected_symbols),
            "source": item.source,
        }
        for item in values
    ]


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise RequestError(f"{field} must be a decimal-compatible value")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise RequestError(f"{field} must be a decimal-compatible value") from exc
    if not parsed.is_finite():
        raise RequestError(f"{field} must be a finite decimal")
    return parsed


def _required(raw: Mapping[str, Any], field: str) -> Any:
    if field not in raw:
        raise RequestError(f"missing required field: {field}")
    return raw[field]


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise RequestError(f"{field} must be an ISO 8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RequestError(f"{field} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RequestError(f"{field} must include a timezone offset")
    return parsed


def _validate_clock_skew(
    timestamp: datetime,
    field: str,
    max_skew_seconds: int = 30,
    reference_time: datetime | None = None,
) -> None:
    reference_time = reference_time or datetime.now(timezone.utc)
    skew = abs(
        (
            reference_time.astimezone(timezone.utc)
            - timestamp.astimezone(timezone.utc)
        ).total_seconds()
    )
    if skew > max_skew_seconds:
        raise RequestError(
            f"{field} differs from server time by more than "
            f"{max_skew_seconds} seconds"
        )


def _enum(enum_type: EnumMeta, value: Any, field: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        members: Mapping[str, Any] = enum_type.__members__
        allowed = ", ".join(
            str(item.value) for item in members.values()
        )
        raise RequestError(f"{field} must be one of: {allowed}") from exc


def _age_seconds(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RequestError(f"{field} must be a non-negative integer")
    if not isinstance(value, int):
        raise RequestError(f"{field} must be a non-negative integer")
    result = value
    if result < 0:
        raise RequestError(f"{field} must be a non-negative integer")
    return result


def _calendar_coverage(
    payload: Mapping[str, Any],
) -> tuple[datetime, datetime]:
    coverage_start = _timestamp(
        _required(payload, "coverage_start"),
        "coverage_start",
    ).astimezone(timezone.utc)
    coverage_end = _timestamp(
        _required(payload, "coverage_end"),
        "coverage_end",
    ).astimezone(timezone.utc)
    if coverage_end <= coverage_start:
        raise RequestError("coverage_end must be after coverage_start")
    if coverage_end - coverage_start > timedelta(days=366):
        raise RequestError("calendar coverage cannot exceed 366 days")
    return coverage_start, coverage_end


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RequestError(f"{field} must be a non-negative integer")
    return value


def _snapshot(raw: Mapping[str, Any]) -> AccountSnapshot:
    if not isinstance(raw, Mapping):
        raise RequestError("snapshot must be a JSON object")
    raw_age = raw.get("data_age_seconds", 0)
    parsed_age = _age_seconds(raw_age, "data_age_seconds")
    if parsed_age is None:
        raise RequestError("data_age_seconds must be a non-negative integer")
    snapshot = AccountSnapshot(
        initial_capital=_decimal(
            _required(raw, "initial_capital"),
            "initial_capital",
        ),
        day_start_balance=_decimal(
            _required(raw, "day_start_balance"), "day_start_balance"
        ),
        highest_settled_balance=_decimal(
            _required(raw, "highest_settled_balance"),
            "highest_settled_balance",
        ),
        balance=_decimal(_required(raw, "balance"), "balance"),
        equity=_decimal(_required(raw, "equity"), "equity"),
        as_of=_timestamp(_required(raw, "as_of"), "as_of"),
        current_open_risk=_decimal(
            raw.get("current_open_risk", "0"), "current_open_risk"
        ),
        data_age_seconds=parsed_age,
        data_uncertain=bool(raw.get("data_uncertain", False)),
        day_locked=bool(raw.get("day_locked", False)),
        breach_latched=bool(raw.get("breach_latched", False)),
        open_positions_count=_optional_nonnegative_int(
            raw.get("open_positions_count"),
            "open_positions_count",
        ),
        pending_orders_count=_optional_nonnegative_int(
            raw.get("pending_orders_count"),
            "pending_orders_count",
        ),
    )
    if snapshot.initial_capital <= 0:
        raise RequestError("initial_capital must be positive")
    if snapshot.day_start_balance <= 0:
        raise RequestError("day_start_balance must be positive")
    if snapshot.highest_settled_balance <= 0:
        raise RequestError("highest_settled_balance must be positive")
    if snapshot.current_open_risk < 0:
        raise RequestError("current_open_risk cannot be negative")
    if snapshot.data_age_seconds < 0:
        raise RequestError("data_age_seconds cannot be negative")
    if not isinstance(raw.get("data_uncertain", False), bool):
        raise RequestError("data_uncertain must be a JSON boolean")
    if not isinstance(raw.get("day_locked", False), bool):
        raise RequestError("day_locked must be a JSON boolean")
    if not isinstance(raw.get("breach_latched", False), bool):
        raise RequestError("breach_latched must be a JSON boolean")
    return snapshot


def _trade_request(raw: Mapping[str, Any]) -> TradeRequest:
    if not isinstance(raw, Mapping):
        raise RequestError("request must be a JSON object")
    action = _enum(Action, _required(raw, "action"), "action")
    risk_increasing = raw.get(
        "is_risk_increasing",
        action in {Action.OPEN, Action.MODIFY},
    )
    if not isinstance(risk_increasing, bool):
        raise RequestError("is_risk_increasing must be a JSON boolean")
    if action == Action.OPEN and not risk_increasing:
        raise RequestError("open requests must be risk-increasing")
    if action in {Action.CLOSE, Action.CANCEL} and risk_increasing:
        raise RequestError(
            "close and cancel requests must be risk-reducing"
        )
    raw_symbol = _required(raw, "symbol")
    if not isinstance(raw_symbol, str):
        raise RequestError("symbol must be a string")
    symbol = raw_symbol.strip()
    if not symbol or len(symbol) > 64:
        raise RequestError("symbol must contain 1 to 64 characters")
    request = TradeRequest(
        symbol=symbol,
        action=action,
        requested_at=_timestamp(_required(raw, "requested_at"), "requested_at"),
        volume=_decimal(raw.get("volume", "0"), "volume"),
        entry_price=(
            _decimal(raw["entry_price"], "entry_price")
            if raw.get("entry_price") is not None
            else None
        ),
        stop_loss=(
            _decimal(raw["stop_loss"], "stop_loss")
            if raw.get("stop_loss") is not None
            else None
        ),
        loss_per_volume_unit=(
            _decimal(
                raw["loss_per_volume_unit"],
                "loss_per_volume_unit",
            )
            if raw.get("loss_per_volume_unit") is not None
            else None
        ),
        estimated_costs=_decimal(
            raw.get("estimated_costs", "0"), "estimated_costs"
        ),
        additional_risk=_decimal(
            raw.get("additional_risk", "0"),
            "additional_risk",
        ),
        is_risk_increasing=risk_increasing,
        idea_id=str(raw.get("idea_id", "")),
    )
    if request.estimated_costs < 0:
        raise RequestError("estimated_costs cannot be negative")
    if request.additional_risk < 0:
        raise RequestError("additional_risk cannot be negative")
    if request.volume < 0:
        raise RequestError("volume cannot be negative")
    if request.entry_price is not None and request.entry_price <= 0:
        raise RequestError("entry_price must be positive when supplied")
    if request.stop_loss is not None and request.stop_loss <= 0:
        raise RequestError("stop_loss must be positive when supplied")
    if (
        request.action == Action.MODIFY
        and request.additional_risk > 0
        and not request.is_risk_increasing
    ):
        raise RequestError(
            "a modification with additional risk must be risk-increasing"
        )
    if (
        request.action == Action.MODIFY
        and request.is_risk_increasing
        and (request.stop_loss is None or request.stop_loss <= 0)
    ):
        raise RequestError(
            "risk-increasing modifications require a positive stop_loss"
        )
    if request.is_open:
        if request.volume <= 0:
            raise RequestError("opening volume must be positive")
        if request.entry_price is None or request.entry_price <= 0:
            raise RequestError("opening requests require a positive entry price")
    return request


def _frequency(raw: Mapping[str, Any] | None) -> FrequencyState:
    raw = raw or {}
    if not isinstance(raw, Mapping):
        raise RequestError("frequency must be a JSON object")
    open_times = raw.get("open_times", [])
    request_times = raw.get("request_times", [])
    last_modify = raw.get("last_modify_by_symbol", {})
    if not isinstance(open_times, list) or len(open_times) > 5000:
        raise RequestError("frequency.open_times must be a list of at most 5000")
    if not isinstance(request_times, list) or len(request_times) > 5000:
        raise RequestError(
            "frequency.request_times must be a list of at most 5000"
        )
    if not isinstance(last_modify, dict) or len(last_modify) > 1000:
        raise RequestError(
            "frequency.last_modify_by_symbol must be an object of at most 1000"
        )
    return FrequencyState(
        open_times=[
            _timestamp(item, "frequency.open_times")
            for item in open_times
        ],
        request_times=[
            _timestamp(item, "frequency.request_times")
            for item in request_times
        ],
        last_modify_by_symbol={
            str(symbol).upper(): _timestamp(value, "frequency.last_modify")
            for symbol, value in last_modify.items()
        },
    )


def _affected_symbols(raw: Any, field: str) -> frozenset[str]:
    if not isinstance(raw, list) or len(raw) > 100:
        raise RequestError(f"{field} must be a list of at most 100 symbols")
    symbols: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise RequestError(f"{field} entries must be strings")
        symbol = item.strip()
        if not 1 <= len(symbol) <= 64:
            raise RequestError(
                f"{field} entries must contain 1 to 64 characters"
            )
        if not re.fullmatch(r"(?:[A-Za-z0-9._:/#-]+\*?|\*)", symbol):
            raise RequestError(
                f"{field} entries may use broker symbol characters and only "
                "one optional trailing wildcard"
            )
        symbols.add(symbol.upper())
    return frozenset(symbols)


def _news_events(raw: list[Mapping[str, Any]] | None) -> list[NewsEvent]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 500:
        raise RequestError("news_events must contain at most 500 events")
    if any(not isinstance(item, Mapping) for item in raw):
        raise RequestError("each news event must be a JSON object")
    events = []
    event_ids: set[str] = set()
    for item in raw:
        event_id = str(_required(item, "event_id")).strip()
        if not 1 <= len(event_id) <= 128:
            raise RequestError(
                "news event_id must contain 1 to 128 characters"
            )
        if event_id in event_ids:
            raise RequestError(f"duplicate news event_id: {event_id}")
        event_ids.add(event_id)
        events.append(
            NewsEvent(
                event_id=event_id,
                release_time=_timestamp(
                    _required(item, "release_time"),
                    "news.release_time",
                ),
                affected_symbols=_affected_symbols(
                    item.get("affected_symbols", []),
                    "affected_symbols",
                ),
                importance=str(item.get("importance", "high")),
                source=str(item.get("source", "ftmo-calendar")),
            )
        )
    return sorted(
        events,
        key=lambda event: (
            event.release_time.astimezone(timezone.utc),
            event.event_id,
        ),
    )


def _market_closures(
    raw: list[Mapping[str, Any]] | None,
) -> list[MarketClosure]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 1000:
        raise RequestError("market_closures must contain at most 1000 closures")
    if any(not isinstance(item, Mapping) for item in raw):
        raise RequestError("each market closure must be a JSON object")
    closures = []
    closure_ids: set[str] = set()
    for item in raw or []:
        closure_id = str(_required(item, "closure_id")).strip()
        if not 1 <= len(closure_id) <= 128:
            raise RequestError(
                "market closure_id must contain 1 to 128 characters"
            )
        if closure_id in closure_ids:
            raise RequestError(
                f"duplicate market closure_id: {closure_id}"
            )
        closure_ids.add(closure_id)
        start_time = _timestamp(
            _required(item, "start_time"),
            "market.start_time",
        )
        end_time = _timestamp(
            _required(item, "end_time"),
            "market.end_time",
        )
        if end_time <= start_time:
            raise RequestError("market.end_time must be after start_time")
        closures.append(
            MarketClosure(
                closure_id=closure_id,
                start_time=start_time,
                end_time=end_time,
                affected_symbols=_affected_symbols(
                    item.get("affected_symbols", []),
                    "affected_symbols",
                ),
                source=str(
                    item.get("source", "approved-market-schedule")
                ),
            )
        )
    return sorted(
        closures,
        key=lambda closure: (
            closure.start_time.astimezone(timezone.utc),
            closure.closure_id,
        ),
    )


def _validate_news_coverage(
    events: list[NewsEvent],
    coverage_start: datetime,
    coverage_end: datetime,
) -> None:
    outside = [
        event.event_id
        for event in events
        if not coverage_start <= event.release_time.astimezone(timezone.utc)
        <= coverage_end
    ]
    if outside:
        raise RequestError(
            "news events fall outside the declared coverage interval: "
            + ", ".join(outside[:5])
        )


def _validate_market_coverage(
    closures: list[MarketClosure],
    coverage_start: datetime,
    coverage_end: datetime,
) -> None:
    outside = [
        closure.closure_id
        for closure in closures
        if closure.end_time.astimezone(timezone.utc) < coverage_start
        or closure.start_time.astimezone(timezone.utc) > coverage_end
    ]
    if outside:
        raise RequestError(
            "market closures do not intersect the declared coverage interval: "
            + ", ".join(outside[:5])
        )


def _profile(
    config_source: ConfigSource,
    raw: Mapping[str, Any],
) -> tuple[RuleProfile, str]:
    account_type = _enum(
        AccountType,
        _required(raw, "account_type"),
        "account_type",
    )
    phase = _enum(AccountPhase, _required(raw, "phase"), "phase")
    style = _enum(AccountStyle, _required(raw, "style"), "style")
    config = _load_config(config_source)
    return (
        RuleProfile.from_config(config, account_type, phase, style),
        str(config.get("rule_version", "unknown")),
    )


def _load_config(config_source: ConfigSource) -> Mapping[str, Any]:
    if isinstance(config_source, Mapping):
        return config_source
    with Path(config_source).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _account_id(value: Any) -> str:
    if not isinstance(value, str):
        raise RequestError("account_id must be a string")
    result = value
    if not result or len(result) > 128:
        raise RequestError("account_id must contain 1 to 128 characters")
    if not all(character.isalnum() or character in "._:-" for character in result):
        raise RequestError(
            "account_id may only contain letters, digits, dot, underscore, "
            "colon, and dash"
        )
    return result


def _request_id(value: Any) -> str:
    if not isinstance(value, str):
        raise RequestError("request_id must be a string")
    result = value
    if not 1 <= len(result) <= 128:
        raise RequestError("request_id must contain 1 to 128 characters")
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", result):
        raise RequestError(
            "request_id may only contain ASCII letters, digits, dot, "
            "underscore, colon, and dash"
        )
    return result


def _stored_snapshot(account: StoredAccount) -> dict[str, Any]:
    snapshot = account.snapshot
    return _jsonable(
        {
            "initial_capital": snapshot.initial_capital,
            "day_start_balance": snapshot.day_start_balance,
            "highest_settled_balance": snapshot.highest_settled_balance,
            "balance": snapshot.balance,
            "equity": snapshot.equity,
            "as_of": snapshot.as_of.isoformat(),
            "current_open_risk": snapshot.current_open_risk,
            "data_age_seconds": snapshot.data_age_seconds,
            "data_uncertain": snapshot.data_uncertain,
            "day_locked": snapshot.day_locked,
            "breach_latched": snapshot.breach_latched,
            "open_positions_count": snapshot.open_positions_count,
            "pending_orders_count": snapshot.pending_orders_count,
        }
    )


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prometheus_label(value: Any) -> str:
    """Escape a value for a Prometheus double-quoted label."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _is_loopback_host(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(
        address[4][0] in {"127.0.0.1", "::1"}
        for address in addresses
    )


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_state_server_lock(state_path: str | Path) -> Path:
    state = Path(state_path).expanduser().resolve()
    lock_path = state.with_name(state.name + ".server.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(
                lock_path,
                flags,
                0o600,
            )
        except FileExistsError:
            try:
                metadata = os.stat(lock_path, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError(
                    "unable to inspect the existing state database lock"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("state database lock must be a regular file")
            if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
                raise ValueError(
                    "state database lock must be owned by this user"
                )
            try:
                read_flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    read_flags |= os.O_NOFOLLOW
                read_descriptor = os.open(lock_path, read_flags)
                try:
                    contents = os.read(read_descriptor, 64).decode("ascii")
                finally:
                    os.close(read_descriptor)
                pid = int(contents.strip())
            except (OSError, UnicodeDecodeError, ValueError):
                pid = -1
            if _process_alive(pid):
                raise ValueError(
                    f"state database is already owned by server process {pid}"
                )
            lock_path.unlink(missing_ok=True)
            continue
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            lock_path.unlink(missing_ok=True)
            raise
        return lock_path
    raise ValueError("unable to acquire state database server lock")


def account_sync_payload(
    payload: Mapping[str, Any],
    state_store: StateStore,
    config_source: ConfigSource,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    as_of = _timestamp(_required(payload, "as_of"), "as_of")
    received_at = datetime.now(timezone.utc)
    _validate_clock_skew(
        as_of,
        "as_of",
        reference_time=received_at,
    )
    initial_capital = _decimal(
        _required(payload, "initial_capital"),
        "initial_capital",
    )
    balance = _decimal(_required(payload, "balance"), "balance")
    equity = _decimal(_required(payload, "equity"), "equity")
    current_open_risk = _decimal(
        payload.get("current_open_risk", "0"),
        "current_open_risk",
    )
    if initial_capital <= 0:
        raise RequestError("initial_capital must be positive")
    if current_open_risk < 0:
        raise RequestError("current_open_risk cannot be negative")
    open_positions_count = _optional_nonnegative_int(
        payload.get("open_positions_count"),
        "open_positions_count",
    )
    pending_orders_count = _optional_nonnegative_int(
        payload.get("pending_orders_count"),
        "pending_orders_count",
    )

    account_type = _enum(
        AccountType,
        _required(payload, "account_type"),
        "account_type",
    )
    phase = _enum(AccountPhase, _required(payload, "phase"), "phase")
    style = _enum(AccountStyle, _required(payload, "style"), "style")
    profile, _ = _profile(
        config_source,
        {
            "account_type": account_type.value,
            "phase": phase.value,
            "style": style.value,
        },
    )
    account = state_store.sync_account(
        account_id=account_id,
        account_type=account_type,
        phase=phase,
        style=style,
        initial_capital=initial_capital,
        balance=balance,
        equity=equity,
        current_open_risk=current_open_risk,
        open_positions_count=open_positions_count,
        pending_orders_count=pending_orders_count,
        as_of=as_of,
        received_at=received_at,
        profile=profile,
        bootstrap_day_start_balance=(
            _decimal(payload["day_start_balance"], "day_start_balance")
            if payload.get("day_start_balance") is not None
            else None
        ),
        bootstrap_highest_settled_balance=(
            _decimal(
                payload["highest_settled_balance"],
                "highest_settled_balance",
            )
            if payload.get("highest_settled_balance") is not None
            else None
        ),
    )
    return {
        "ok": True,
        "account_id": account_id,
        "ftmo_day": account.ftmo_day,
        "snapshot": _stored_snapshot(account),
    }


def settlement_sync_payload(
    payload: Mapping[str, Any],
    state_store: StateStore,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    ftmo_day = str(_required(payload, "ftmo_day"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ftmo_day):
        raise RequestError("ftmo_day must use YYYY-MM-DD format")
    settled_balance = _decimal(
        _required(payload, "settled_balance"),
        "settled_balance",
    )
    settled_at = _timestamp(_required(payload, "settled_at"), "settled_at")
    if (
        settled_at.astimezone(timezone.utc)
        > datetime.now(timezone.utc) + timedelta(seconds=30)
    ):
        raise RequestError(
            "settled_at is more than 30 seconds in the future"
        )
    if ftmo_day_key(settled_at, state_store.day_timezone) != ftmo_day:
        raise RequestError(
            "settled_at must belong to the supplied ftmo_day in the configured "
            "day timezone"
        )
    source = str(payload.get("source", "platform-settlement")).strip()
    if len(source) > 128:
        raise RequestError("source must contain at most 128 characters")
    account = state_store.confirm_settlement(
        account_id=account_id,
        ftmo_day=ftmo_day,
        settled_balance=settled_balance,
        settled_at=settled_at,
        source=source,
    )
    return {
        "ok": True,
        "account_id": account_id,
        "ftmo_day": ftmo_day,
        "snapshot": _stored_snapshot(account),
    }


def evaluate_stored_payload(
    payload: Mapping[str, Any],
    config_source: ConfigSource,
    state_store: StateStore,
    request_id: str,
    default_news_events: list[NewsEvent],
    default_news_age_seconds: int | None,
    default_market_closures: list[MarketClosure],
    default_market_age_seconds: int | None,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    request = _trade_request(_required(payload, "request"))
    _validate_clock_skew(request.requested_at, "request.requested_at")
    received_at = datetime.now(timezone.utc)
    request_hash = _canonical_hash(
        {
            "account_id": account_id,
            "request": payload["request"],
        }
    )

    def evaluator(
        account: StoredAccount,
        frequency: FrequencyState,
    ) -> Mapping[str, Any]:
        merged = dict(payload)
        server_request = dict(payload["request"])
        server_request["requested_at"] = received_at.isoformat()
        merged.update(
            {
                "account_type": account.account_type.value,
                "phase": account.phase.value,
                "style": account.style.value,
                "snapshot": _stored_snapshot(account),
                "request": server_request,
                "frequency": {
                    "open_times": [
                        item.isoformat() for item in frequency.open_times
                    ],
                    "request_times": [
                        item.isoformat() for item in frequency.request_times
                    ],
                    "last_modify_by_symbol": {
                        symbol: item.isoformat()
                        for symbol, item in (
                            frequency.last_modify_by_symbol.items()
                        )
                    },
                },
            }
        )
        # Calendars and freshness are server-authoritative in stateful mode.
        for key in (
            "news_events",
            "news_data_age_seconds",
            "market_closures",
            "market_data_age_seconds",
        ):
            merged.pop(key, None)
        response = evaluate_payload(
            merged,
            config_source,
            default_news_events=default_news_events,
            default_news_age_seconds=default_news_age_seconds,
            default_market_closures=default_market_closures,
            default_market_age_seconds=default_market_age_seconds,
            day_timezone=state_store.day_timezone,
        )
        response["account_id"] = account_id
        return response

    return state_store.evaluate_and_reserve(
        account_id=account_id,
        request_id=request_id,
        request_hash=request_hash,
        action=request.action.value,
        symbol=request.symbol,
        occurred_at=received_at,
        evaluator=evaluator,
        block_on_unknown_execution=request.is_risk_increasing,
    )


def execution_result_payload(
    payload: Mapping[str, Any],
    state_store: StateStore,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    request_id = _request_id(_required(payload, "request_id"))
    raw_outcome = payload.get("outcome")
    if raw_outcome is not None:
        if raw_outcome not in {"success", "failure", "unknown"}:
            raise RequestError(
                "outcome must be success, failure, or unknown"
            )
        if "success" in payload:
            raw_success = payload["success"]
            if not isinstance(raw_success, bool):
                raise RequestError("success must be a JSON boolean")
            expected = "success" if raw_success else "failure"
            if raw_outcome != expected:
                raise RequestError("success and outcome disagree")
        outcome = str(raw_outcome)
    else:
        raw_success = _required(payload, "success")
        if not isinstance(raw_success, bool):
            raise RequestError("success must be a JSON boolean")
        outcome = "success" if raw_success else "failure"
    action = _enum(Action, _required(payload, "action"), "action")
    symbol = str(_required(payload, "symbol")).strip()
    if not symbol:
        raise RequestError("symbol must not be empty")
    occurred_at = _timestamp(
        _required(payload, "occurred_at"),
        "occurred_at",
    )
    received_at = datetime.now(timezone.utc)
    if occurred_at.astimezone(timezone.utc) > (
        received_at + timedelta(seconds=30)
    ):
        raise RequestError(
            "occurred_at is more than 30 seconds in the future"
        )
    platform_status = str(payload.get("platform_status", ""))
    platform_order_id = str(payload.get("platform_order_id", ""))
    detail = json.dumps(
        {
            "outcome": outcome,
            "action": action.value,
            "platform_status": platform_status,
            "platform_order_id": platform_order_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    execution_identity = {
        "account_id": account_id,
        "request_id": request_id,
        "action": action.value,
        "symbol": symbol.upper(),
        "outcome": outcome,
        "platform_status": platform_status,
        "platform_order_id": platform_order_id,
    }
    return state_store.record_execution(
        account_id=account_id,
        request_id=request_id,
        request_hash=_canonical_hash(execution_identity),
        action=action.value,
        symbol=symbol,
        occurred_at=received_at,
        outcome=outcome,
        detail=detail,
    )


def news_status_payload(
    payload: Mapping[str, Any],
    state_store: StateStore,
    news_events: list[NewsEvent],
    news_age_seconds: int | None,
    config_source: ConfigSource,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    symbol = str(_required(payload, "symbol")).upper()
    client_now = _timestamp(_required(payload, "now"), "now")
    _validate_clock_skew(client_now, "now")
    now = datetime.now(timezone.utc)
    account = state_store.get_account(account_id)
    profile, rule_version = _profile(
        config_source,
        {
            "account_type": account.account_type.value,
            "phase": account.phase.value,
            "style": account.style.value,
        },
    )
    is_restricted_account = (
        profile.phase == AccountPhase.FTMO_ACCOUNT
        and profile.style == AccountStyle.STANDARD
    )
    stale = (
        news_age_seconds is None
        or news_age_seconds > profile.news_max_calendar_age_seconds
    )
    if stale:
        return {
            "ok": True,
            "rule_version": rule_version,
            "account_id": account_id,
            "symbol": symbol,
            "news_data_stale": True,
            "open_blocked": True,
            "force_flat": False,
            "cancel_pending": True,
            "hard_window": False,
            "emergency_alert": True,
            "event_ids": [],
        }

    open_blocked = False
    force_flat = False
    cancel_pending = False
    hard_window = False
    event_ids: list[str] = []
    for event in news_events:
        if not event.affects(symbol):
            continue
        delta = event.release_time - now
        internal_before = timedelta(
            minutes=profile.news_internal_before_minutes
        )
        internal_after = timedelta(
            minutes=profile.news_internal_after_minutes
        )
        hard_before = timedelta(minutes=profile.news_hard_before_minutes)
        hard_after = timedelta(minutes=profile.news_hard_after_minutes)
        force_flat_before = timedelta(
            minutes=profile.news_force_flat_before_minutes
        )
        cancel_pending_before = timedelta(
            minutes=profile.news_cancel_pending_before_minutes
        )

        if -internal_after <= delta <= internal_before:
            open_blocked = True
            cancel_pending = True
            event_ids.append(event.event_id)
        if is_restricted_account and -hard_after <= delta <= hard_before:
            hard_window = True
            cancel_pending = True
        if (
            is_restricted_account
            and hard_before < delta <= force_flat_before
        ):
            force_flat = True
        if (
            is_restricted_account
            and hard_before < delta <= cancel_pending_before
        ):
            cancel_pending = True

    return {
        "ok": True,
        "rule_version": rule_version,
        "account_id": account_id,
        "symbol": symbol,
        "news_data_stale": False,
        "open_blocked": open_blocked,
        "force_flat": force_flat,
        "cancel_pending": cancel_pending,
        "hard_window": hard_window,
        "emergency_alert": hard_window,
        "event_ids": sorted(set(event_ids)),
    }


def market_status_payload(
    payload: Mapping[str, Any],
    state_store: StateStore,
    closures: list[MarketClosure],
    market_age_seconds: int | None,
    config_source: ConfigSource,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    symbol = str(_required(payload, "symbol")).upper()
    client_now = _timestamp(_required(payload, "now"), "now")
    _validate_clock_skew(client_now, "now")
    now = datetime.now(timezone.utc)
    account = state_store.get_account(account_id)
    profile, rule_version = _profile(
        config_source,
        {
            "account_type": account.account_type.value,
            "phase": account.phase.value,
            "style": account.style.value,
        },
    )
    is_restricted_account = (
        profile.phase == AccountPhase.FTMO_ACCOUNT
        and profile.style == AccountStyle.STANDARD
    )
    config = _load_config(config_source)
    max_age = int(
        config.get("market_close_controls", {}).get(
            "max_schedule_age_seconds",
            3600,
        )
    )
    stale = (
        market_age_seconds is None
        or market_age_seconds > max_age
    )
    if stale:
        return {
            "ok": True,
            "rule_version": rule_version,
            "account_id": account_id,
            "symbol": symbol,
            "market_data_stale": True,
            "open_blocked": True,
            "force_flat": False,
            "cancel_pending": True,
            "closure_active": False,
            "emergency_alert": True,
            "closure_ids": [],
        }

    open_blocked = False
    force_flat = False
    cancel_pending = False
    closure_active = False
    closure_ids = []
    flat_before = timedelta(minutes=profile.market_flat_before_minutes)
    open_block_before = timedelta(
        minutes=profile.market_gap_open_block_before_minutes
    )
    restricted_duration = timedelta(
        minutes=profile.market_restricted_break_min_minutes
    )
    for closure in closures:
        if not closure.affects(symbol):
            continue
        if closure.end_time - closure.start_time < restricted_duration:
            continue
        until_start = closure.start_time - now
        if closure.start_time <= now <= closure.end_time:
            closure_active = True
            open_blocked = True
            cancel_pending = True
            closure_ids.append(closure.closure_id)
        elif timedelta(0) <= until_start <= open_block_before:
            open_blocked = True
            cancel_pending = True
            if (
                is_restricted_account
                and until_start <= flat_before
            ):
                force_flat = True
                cancel_pending = True
            closure_ids.append(closure.closure_id)

    return {
        "ok": True,
        "rule_version": rule_version,
        "account_id": account_id,
        "symbol": symbol,
        "market_data_stale": False,
        "open_blocked": open_blocked,
        "force_flat": force_flat,
        "cancel_pending": cancel_pending,
        "closure_active": closure_active,
        "emergency_alert": closure_active,
        "closure_ids": sorted(set(closure_ids)),
    }


def _credential_json(record: Any) -> dict[str, Any]:
    return {
        "credential_id": record.credential_id,
        "account_id": record.account_id,
        "not_before": record.not_before.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "revoked_at": (
            record.revoked_at.isoformat()
            if record.revoked_at is not None
            else None
        ),
        "last_used_at": (
            record.last_used_at.isoformat()
            if record.last_used_at is not None
            else None
        ),
        "scopes": list(record.scopes),
        "created_at": record.created_at.isoformat(),
    }


def _credential_scopes(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return tuple(sorted(PLATFORM_CREDENTIAL_SCOPES))
    if not isinstance(raw, list) or not raw:
        raise RequestError("scopes must be a non-empty JSON list")
    scopes = tuple(sorted({str(item).strip() for item in raw if str(item).strip()}))
    if not scopes or any(len(item) > 64 for item in scopes):
        raise RequestError("scopes must contain non-empty values of at most 64 characters")
    if any(item not in ACCOUNT_CREDENTIAL_SCOPES for item in scopes):
        raise RequestError("scopes contain an unsupported account scope")
    return scopes


def _credential_expiry(
    payload: Mapping[str, Any],
    config_source: ConfigSource,
    now: datetime,
) -> tuple[datetime, datetime]:
    security = _load_config(config_source).get("security", {})
    default_ttl = int(security.get("credential_default_ttl_seconds", 2592000))
    max_ttl = int(security.get("credential_max_ttl_seconds", 7776000))
    not_before = (
        _timestamp(payload["not_before"], "not_before")
        if payload.get("not_before") is not None
        else now
    )
    expires_at = (
        _timestamp(payload["expires_at"], "expires_at")
        if payload.get("expires_at") is not None
        else now + timedelta(seconds=default_ttl)
    )
    if expires_at <= not_before:
        raise RequestError("expires_at must be after not_before")
    if expires_at > now + timedelta(seconds=max_ttl):
        raise RequestError("credential expiry exceeds configured maximum TTL")
    if not_before > now + timedelta(seconds=30):
        raise RequestError("not_before cannot be more than 30 seconds in the future")
    return not_before, expires_at


def create_credential_payload(
    *,
    account_id: str,
    payload: Mapping[str, Any],
    state_store: StateStore,
    config_source: ConfigSource,
) -> dict[str, Any]:
    try:
        state_store.get_account(account_id)
    except KeyError as exc:
        raise RequestError(
            "account must be provisioned before an account credential is issued"
        ) from exc
    now = datetime.now(timezone.utc)
    not_before, expires_at = _credential_expiry(payload, config_source, now)
    record, secret = state_store.create_account_credential(
        account_id=account_id,
        scopes=_credential_scopes(payload.get("scopes")),
        not_before=not_before,
        expires_at=expires_at,
        now=now,
    )
    return {
        "ok": True,
        "credential": _credential_json(record),
        "secret": secret,
        "secret_disclosure": "shown_once",
    }


def rotate_credential_payload(
    *,
    credential_id: str,
    payload: Mapping[str, Any],
    state_store: StateStore,
    config_source: ConfigSource,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    not_before, expires_at = _credential_expiry(payload, config_source, now)
    security = _load_config(config_source).get("security", {})
    overlap_seconds = int(
        payload.get(
            "overlap_seconds",
            security.get("credential_rotation_overlap_seconds", 300),
        )
    )
    if overlap_seconds < 0 or overlap_seconds > 86400:
        raise RequestError("overlap_seconds must be between 0 and 86400")
    record, secret = state_store.rotate_account_credential(
        credential_id=credential_id,
        scopes=(
            _credential_scopes(payload["scopes"])
            if payload.get("scopes") is not None
            else None
        ),
        not_before=not_before,
        expires_at=expires_at,
        overlap_seconds=overlap_seconds,
        now=now,
    )
    return {
        "ok": True,
        "credential": _credential_json(record),
        "secret": secret,
        "secret_disclosure": "shown_once",
        "old_credential_overlap_seconds": overlap_seconds,
    }


def revoke_credential_payload(
    credential_id: str,
    state_store: StateStore,
) -> dict[str, Any]:
    record = state_store.revoke_account_credential(credential_id)
    return {"ok": True, "credential": _credential_json(record)}


def list_credentials_payload(
    *,
    account_id: str | None,
    state_store: StateStore,
) -> dict[str, Any]:
    records = state_store.list_account_credentials(account_id)
    return {
        "ok": True,
        "account_id": account_id,
        "credentials": [_credential_json(record) for record in records],
    }


def closed_trade_sync_payload(
    *,
    payload: Mapping[str, Any],
    state_store: StateStore,
    request_id: str,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    trade_id = str(_required(payload, "trade_id")).strip()
    if not 1 <= len(trade_id) <= 160:
        raise RequestError("trade_id must contain 1 to 160 characters")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+", trade_id):
        raise RequestError("trade_id contains unsupported characters")
    closed_at = _timestamp(_required(payload, "closed_at"), "closed_at")
    if closed_at.astimezone(timezone.utc) > (
        datetime.now(timezone.utc) + timedelta(seconds=30)
    ):
        raise RequestError("closed_at cannot be more than 30 seconds in the future")
    net_profit = _decimal(_required(payload, "net_profit"), "net_profit")
    phase = _enum(
        AccountPhase,
        _required(payload, "phase"),
        "phase",
    )
    cycle_id = _request_id(_required(payload, "cycle_id"))
    symbol = str(payload.get("symbol", "")).strip().upper()
    source = str(payload.get("source", "platform-history")).strip()
    if len(symbol) > 64:
        raise RequestError("symbol must contain at most 64 characters")
    if not source or len(source) > 128:
        raise RequestError("source must contain 1 to 128 characters")
    result = state_store.record_closed_trade(
        account_id=account_id,
        trade_id=trade_id,
        phase=phase,
        cycle_id=cycle_id,
        closed_at=closed_at,
        ftmo_day=ftmo_day_key(closed_at, state_store.day_timezone),
        net_profit=net_profit,
        symbol=symbol,
        source=source,
        request_id=_request_id(str(payload.get("request_id", request_id))),
    )
    return result


def qualification_history_sync_payload(
    *,
    payload: Mapping[str, Any],
    state_store: StateStore,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    phase = _enum(
        AccountPhase,
        _required(payload, "phase"),
        "phase",
    )
    cycle_id = _request_id(_required(payload, "cycle_id"))
    history_start_at = _timestamp(
        _required(payload, "history_start_at"),
        "history_start_at",
    )
    complete_through = _timestamp(
        _required(payload, "complete_through"),
        "complete_through",
    )
    if complete_through.astimezone(timezone.utc) > (
        datetime.now(timezone.utc) + timedelta(seconds=30)
    ):
        raise RequestError(
            "complete_through cannot be more than 30 seconds in the future"
        )
    source = str(payload.get("source", "platform-history")).strip()
    if not source or len(source) > 128:
        raise RequestError("source must contain 1 to 128 characters")
    status = state_store.set_qualification_history_status(
        account_id=account_id,
        phase=phase,
        cycle_id=cycle_id,
        history_start_at=history_start_at,
        complete_through=complete_through,
        source=source,
    )
    return {"ok": True, "history": status}


def trading_day_sync_payload(
    *,
    payload: Mapping[str, Any],
    state_store: StateStore,
    request_id: str,
) -> dict[str, Any]:
    account_id = _account_id(_required(payload, "account_id"))
    phase = _enum(
        AccountPhase,
        _required(payload, "phase"),
        "phase",
    )
    cycle_id = _request_id(_required(payload, "cycle_id"))
    opened_at = _timestamp(_required(payload, "opened_at"), "opened_at")
    if opened_at.astimezone(timezone.utc) > (
        datetime.now(timezone.utc) + timedelta(seconds=30)
    ):
        raise RequestError(
            "opened_at cannot be more than 30 seconds in the future"
        )
    source = str(payload.get("source", "platform-history")).strip()
    if not source or len(source) > 128:
        raise RequestError("source must contain 1 to 128 characters")
    trading_day = state_store.record_qualification_trading_day(
        account_id=account_id,
        phase=phase,
        cycle_id=cycle_id,
        opened_at=opened_at,
        ftmo_day=ftmo_day_key(opened_at, state_store.day_timezone),
        source=source,
        request_id=_request_id(str(payload.get("request_id", request_id))),
    )
    return {"ok": True, "trading_day": trading_day}


def qualification_payload(
    *,
    account_id: str,
    state_store: StateStore,
    config_source: ConfigSource,
) -> dict[str, Any]:
    account = state_store.get_account(account_id)
    history_status = state_store.qualification_history_status(account_id)
    closed_trades = (
        state_store.closed_trades(
            account_id,
            phase=AccountPhase(history_status["phase"]),
            cycle_id=str(history_status["cycle_id"]),
        )
        if history_status is not None
        else []
    )
    trading_day_events = (
        state_store.qualification_trading_days(
            account_id,
            phase=AccountPhase(history_status["phase"]),
            cycle_id=str(history_status["cycle_id"]),
        )
        if history_status is not None
        else []
    )
    return qualification_snapshot(
        config=_load_config(config_source),
        account=account,
        closed_trades=closed_trades,
        trading_day_events=trading_day_events,
        history_status=history_status,
    )


def _decimal_json(value: Decimal) -> str:
    return format(value, "f")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_json(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def evaluate_payload(
    payload: Mapping[str, Any],
    config_source: ConfigSource,
    default_news_events: list[NewsEvent] | None = None,
    default_news_age_seconds: int | None = None,
    default_market_closures: list[MarketClosure] | None = None,
    default_market_age_seconds: int | None = None,
    day_timezone: str = "Europe/Prague",
) -> dict[str, Any]:
    profile, rule_version = _profile(config_source, payload)
    snapshot = _snapshot(_required(payload, "snapshot"))
    request = _trade_request(_required(payload, "request"))
    frequency = _frequency(payload.get("frequency"))
    events = (
        _news_events(payload["news_events"])
        if "news_events" in payload
        else list(default_news_events or [])
    )
    news_age_seconds = payload.get(
        "news_data_age_seconds",
        default_news_age_seconds,
    )
    news_age_seconds = _age_seconds(
        news_age_seconds,
        "news_data_age_seconds",
    )
    closures = (
        _market_closures(payload["market_closures"])
        if "market_closures" in payload
        else list(default_market_closures or [])
    )
    market_age_seconds = payload.get(
        "market_data_age_seconds",
        default_market_age_seconds,
    )
    market_age_seconds = _age_seconds(
        market_age_seconds,
        "market_data_age_seconds",
    )
    config = _load_config(config_source)
    max_market_age = int(
        config.get("market_close_controls", {}).get(
            "max_schedule_age_seconds",
            3600,
        )
    )
    engine = RiskEngine(profile, day_timezone=day_timezone)
    if (
        request.is_risk_increasing
        and (
            news_age_seconds is None
            or news_age_seconds > profile.news_max_calendar_age_seconds
        )
    ):
        decision = Decision(
            DecisionCode.REJECT_DATA_STALE,
            ("news calendar is stale or unavailable",),
        )
    else:
        evaluated = engine.evaluate(
            snapshot,
            request,
            frequency,
            events,
            closures,
        )
        if (
            request.is_risk_increasing
            and (
                market_age_seconds is None
                or market_age_seconds > max_market_age
            )
            and evaluated.code != DecisionCode.REJECT_NEWS
        ):
            decision = Decision(
                DecisionCode.REJECT_DATA_STALE,
                ("market closure schedule is stale or unavailable",),
            )
        else:
            decision = evaluated
    budget = decision.risk_budget
    return _jsonable(
        {
            "ok": True,
            "rule_version": rule_version,
            "decision": {
                "code": decision.code.value,
                "allowed": decision.allowed,
                "reasons": decision.reasons,
                "risk_budget": asdict(budget) if budget else None,
            },
            "account": {
                "status": engine.status(snapshot),
                "daily_loss": engine.daily_loss(snapshot),
                "daily_loss_limit": engine.daily_loss_limit(snapshot),
                "max_loss_limit": engine.max_loss_limit(snapshot),
                "internal_daily_stop_limit": engine.internal_daily_stop_limit(
                    snapshot
                ),
                "internal_max_loss_stop_limit": (
                    engine.internal_max_loss_stop_limit(snapshot)
                ),
                "daily_utilization": engine.daily_utilization(snapshot),
                "max_loss_utilization": engine.max_loss_utilization(snapshot),
            },
        }
    )


def position_size_payload(
    payload: Mapping[str, Any],
    config_source: ConfigSource,
) -> dict[str, Any]:
    profile, rule_version = _profile(config_source, payload)
    snapshot = _snapshot(_required(payload, "snapshot"))
    engine = RiskEngine(profile)
    account_status = engine.status(snapshot)
    if (
        account_status in {"RED", "LOCKED", "BREACH"}
        or snapshot.data_age_seconds > 5
        or snapshot.data_uncertain
    ):
        result = {
            "volume": Decimal("0"),
            "expected_loss": Decimal("0"),
            "risk_budget": Decimal("0"),
        }
    else:
        result = asdict(
            engine.size_position(
                snapshot=snapshot,
                loss_per_volume_unit=_decimal(
                    _required(payload, "loss_per_volume_unit"),
                    "loss_per_volume_unit",
                ),
                volume_step=_decimal(
                    _required(payload, "volume_step"),
                    "volume_step",
                ),
                min_volume=_decimal(
                    _required(payload, "min_volume"),
                    "min_volume",
                ),
                estimated_costs=_decimal(
                    payload.get("estimated_costs", "0"),
                    "estimated_costs",
                ),
                max_volume=(
                    _decimal(payload["max_volume"], "max_volume")
                    if payload.get("max_volume") is not None
                    else None
                ),
            )
        )
    return _jsonable(
        {
            "ok": True,
            "rule_version": rule_version,
            "position_size": result,
            "account_status": account_status,
        }
    )


class RiskRequestHandler(BaseHTTPRequestHandler):
    server_version = "FTMO-RiskAPI/5.0"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.risk_server.read_timeout_seconds)
        self.auth_subject: str | None = None

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the service quiet by default; platform adapters log decisions.
        return

    @property
    def risk_server(self) -> "RiskHTTPServer":
        return self.server  # type: ignore[return-value]

    def _global_authorized(self) -> bool:
        expected = self.risk_server.auth_token
        if not self.risk_server.require_auth:
            return True
        supplied = self.headers.get("X-Risk-Token", "")
        if not supplied:
            authorization = self.headers.get("Authorization", "")
            scheme, separator, value = authorization.partition(" ")
            if separator and scheme.lower() == "bearer":
                supplied = value
        return hmac.compare_digest(supplied, expected)

    def _authorize_account(
        self,
        *,
        account_id: str,
        scope: str,
        allow_admin: bool = False,
    ) -> str:
        if self._global_authorized():
            if (
                allow_admin
                or not self.risk_server.account_credentials_required
            ):
                return "admin"
            raise ForbiddenError(
                "account credential is required for this endpoint"
            )
        secret = self.headers.get("X-Account-Credential", "")
        if self.risk_server.state_store is None:
            raise AuthorizationError("account credential state is unavailable")
        record = self.risk_server.state_store.authenticate_account_credential(
            account_id=account_id,
            secret=secret,
            scope=scope,
        )
        if record is None:
            raise AuthorizationError(
                "invalid, expired, revoked, or out-of-scope account credential"
            )
        return record.credential_id

    def _authorize_payload(self, path: str, payload: Mapping[str, Any]) -> str:
        if path in {"/v1/news-sync", "/v1/market-sync"}:
            if not self._global_authorized():
                raise AuthorizationError("invalid administrator token")
            return "admin"
        if path.startswith("/v1/admin/"):
            if not self._global_authorized():
                raise AuthorizationError("invalid administrator token")
            return "admin"
        if path == "/v1/position-size":
            if not self._global_authorized():
                raise AuthorizationError("invalid administrator token")
            return "admin"
        if path == "/v1/evaluate" and "account_id" not in payload:
            if not self._global_authorized():
                raise AuthorizationError("invalid administrator token")
            return "admin"
        scope = ACCOUNT_SCOPES.get(path)
        if scope is None:
            if self._global_authorized():
                return "admin"
            raise AuthorizationError("invalid authentication credentials")
        account_id = _account_id(_required(payload, "account_id"))
        return self._authorize_account(
            account_id=account_id,
            scope=scope,
            allow_admin=path == "/v1/account-sync",
        )

    def _send_json(self, status: HTTPStatus, body: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            _jsonable(body),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(encoded)
        self.risk_server.observe_response(self.command, self.path, status, body)

    def _audit(
        self,
        request_id: str,
        endpoint: str,
        status: int,
        body: Mapping[str, Any],
    ) -> None:
        self.risk_server.write_audit(
            {
                "request_id": request_id,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "endpoint": _path_only(endpoint),
                "http_status": status,
                "client": self.client_address[0],
                "auth_subject": self.auth_subject,
                "decision": body.get("decision"),
                "account": body.get("account"),
                "rule_version": body.get("rule_version"),
                "event_count": body.get("event_count"),
                "error": body.get("error"),
            }
        )

    def _calendar_sync_failure(self, path: str) -> None:
        if path == "/v1/news-sync":
            self.risk_server.observe_calendar_sync("news", "failure")
        elif path == "/v1/market-sync":
            self.risk_server.observe_calendar_sync("market", "failure")

    def _read_json(self) -> Mapping[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestError("Content-Length must be an integer") from exc
        if length <= 0 or length > self.risk_server.max_body_bytes:
            raise RequestError("request body is empty or too large")
        try:
            body = json.loads(
                self.rfile.read(length),
                object_pairs_hook=_json_object_without_duplicate_keys,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RequestError("request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise RequestError("request body must be a JSON object")
        return body

    def _request_id_from_headers(self) -> str:
        value = self.headers.get("X-Request-Id")
        return _request_id(value if value is not None else str(uuid4()))

    def do_GET(self) -> None:
        path = _path_only(self.path)
        if path == "/dashboard/qualification":
            try:
                encoded = self.risk_server.qualification_dashboard_path.read_bytes()
            except OSError:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": "qualification dashboard is unavailable"},
                )
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(encoded)
            self.risk_server.observe_response(
                self.command,
                self.path,
                HTTPStatus.OK,
                {},
            )
            return
        if path in {"/health", "/ready"}:
            if not self._global_authorized():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "invalid administrator token"},
                )
                return
            self.auth_subject = "admin"
            readiness = self.risk_server.readiness()
            database_up = bool(readiness["database_up"])
            unknown_executions = None
            if database_up and self.risk_server.state_store is not None:
                unknown_executions = (
                    self.risk_server.state_store.unknown_execution_count()
                )
            body = {
                "ok": readiness["ready"] if path == "/ready" else True,
                "service": "ftmo-risk-api",
                "rule_version": self.risk_server.rule_version,
                "ready_for_risk_increase": readiness["ready"],
                "readiness_reasons": readiness["reasons"],
                "news_data_age_seconds": (
                    self.risk_server.news_age_seconds()
                ),
                "market_data_age_seconds": (
                    self.risk_server.market_age_seconds()
                ),
                "news_calendar": self.risk_server.calendar_health("news"),
                "market_calendar": self.risk_server.calendar_health(
                    "market"
                ),
                "persistent_state": self.risk_server.state_store is not None,
                "database_up": database_up,
                "unknown_execution_records": unknown_executions,
                "account_credentials_required": (
                    self.risk_server.account_credentials_required
                ),
                "mtls_enabled": self.risk_server.mtls_enabled,
                "mtls_client_certificate_required": (
                    self.risk_server.require_client_cert
                ),
            }
            self._send_json(
                (
                    HTTPStatus.OK
                    if path == "/health" or readiness["ready"]
                    else HTTPStatus.SERVICE_UNAVAILABLE
                ),
                body,
            )
            return
        if path == "/metrics":
            if not self._global_authorized():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "invalid administrator token"},
                )
                return
            self.auth_subject = "admin"
            encoded = self.risk_server.metrics_text().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                "text/plain; version=0.0.4; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)
            self.risk_server.observe_response(
                self.command,
                self.path,
                HTTPStatus.OK,
                {},
            )
            return
        if path == "/v1/qualification":
            if self.risk_server.state_store is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "persistent state is disabled"},
                )
                return
            query = parse_qs(urlsplit(self.path).query)
            values = query.get("account_id", [])
            if len(values) != 1:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "account_id query parameter is required"},
                )
                return
            try:
                account_id = _account_id(values[0])
                self.auth_subject = self._authorize_account(
                    account_id=account_id,
                    scope="qualification:read",
                )
                body = qualification_payload(
                    account_id=account_id,
                    state_store=self.risk_server.state_store,
                    config_source=self.risk_server.config,
                )
                self._send_json(HTTPStatus.OK, body)
            except ForbiddenError as exc:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"ok": False, "error": str(exc)},
                )
            except (AuthorizationError, KeyError, ValueError) as exc:
                self._send_json(
                    HTTPStatus.UNAUTHORIZED
                    if isinstance(exc, AuthorizationError)
                    else HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": str(exc)},
                )
            return
        if path == "/v1/qualification/accounts":
            if not self._global_authorized():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "invalid administrator token"},
                )
                return
            self.auth_subject = "admin"
            if self.risk_server.state_store is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "persistent state is disabled"},
                )
                return
            accounts = [
                qualification_payload(
                    account_id=account.account_id,
                    state_store=self.risk_server.state_store,
                    config_source=self.risk_server.config,
                )
                for account in self.risk_server.state_store.all_accounts()
            ]
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "rule_version": self.risk_server.rule_version, "accounts": accounts},
            )
            return
        if path == "/v1/admin/credentials":
            if not self._global_authorized():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "invalid administrator token"},
                )
                return
            self.auth_subject = "admin"
            if self.risk_server.state_store is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": "persistent state is disabled"},
                )
                return
            query = parse_qs(urlsplit(self.path).query)
            values = query.get("account_id", [])
            try:
                if len(values) > 1:
                    raise RequestError(
                        "account_id query parameter must not be repeated"
                    )
                credential_account_id: str | None = (
                    _account_id(values[0]) if values else None
                )
                self._send_json(
                    HTTPStatus.OK,
                    list_credentials_payload(
                        account_id=credential_account_id,
                        state_store=self.risk_server.state_store,
                    ),
                )
            except RequestError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": str(exc)},
                )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = _path_only(self.path)
        try:
            request_id = self._request_id_from_headers()
        except RequestError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc)},
            )
            return
        if not self.risk_server.allow_request(self.client_address[0]):
            body = {
                "ok": False,
                "error": "request rate limit exceeded",
                "request_id": request_id,
            }
            self._audit(
                request_id,
                self.path,
                HTTPStatus.TOO_MANY_REQUESTS,
                body,
            )
            self._send_json(HTTPStatus.TOO_MANY_REQUESTS, body)
            return
        try:
            payload = self._read_json()
            self.auth_subject = self._authorize_payload(path, payload)
            if path == "/v1/evaluate":
                with self.risk_server.news_lock:
                    cached_events = list(self.risk_server.news_events)
                    cached_news_age = self.risk_server.calendar_age_for_risk(
                        "news"
                    )
                with self.risk_server.market_lock:
                    cached_closures = list(
                        self.risk_server.market_closures
                    )
                    cached_market_age = (
                        self.risk_server.calendar_age_for_risk("market")
                    )
                if "account_id" in payload:
                    if self.risk_server.state_store is None:
                        raise RequestError(
                            "persistent state is disabled on this server"
                        )
                    body = evaluate_stored_payload(
                        payload,
                        self.risk_server.config,
                        self.risk_server.state_store,
                        request_id,
                        cached_events,
                        cached_news_age,
                        cached_closures,
                        cached_market_age,
                    )
                else:
                    if not self.risk_server.allow_stateless_evaluate:
                        raise EndpointDisabledError(
                            "stateless /v1/evaluate is disabled; use account_id"
                        )
                    sanitized = dict(payload)
                    for key in (
                        "news_events",
                        "news_data_age_seconds",
                        "market_closures",
                        "market_data_age_seconds",
                    ):
                        sanitized.pop(key, None)
                    body = evaluate_payload(
                        sanitized,
                        self.risk_server.config,
                        default_news_events=cached_events,
                        default_news_age_seconds=cached_news_age,
                        default_market_closures=cached_closures,
                        default_market_age_seconds=cached_market_age,
                        day_timezone=self.risk_server.day_timezone,
                    )
            elif path == "/v1/position-size":
                if not self.risk_server.allow_stateless_position_size:
                    raise EndpointDisabledError(
                        "stateless /v1/position-size is disabled on this server"
                    )
                body = position_size_payload(
                    payload,
                    self.risk_server.config,
                )
            elif path == "/v1/account-sync":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                if self.auth_subject != "admin":
                    account_id = _account_id(
                        _required(payload, "account_id")
                    )
                    try:
                        self.risk_server.state_store.get_account(account_id)
                    except KeyError as exc:
                        raise ForbiddenError(
                            "account must be provisioned by an administrator "
                            "before a platform credential can synchronize it"
                        ) from exc
                body = account_sync_payload(
                    payload,
                    self.risk_server.state_store,
                    self.risk_server.config,
                )
            elif path == "/v1/settlement-sync":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                body = settlement_sync_payload(
                    payload,
                    self.risk_server.state_store,
                )
            elif path == "/v1/execution-result":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                body = execution_result_payload(
                    payload,
                    self.risk_server.state_store,
                )
            elif path == "/v1/news-status":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                with self.risk_server.news_lock:
                    cached_events = list(self.risk_server.news_events)
                    cached_news_age = self.risk_server.calendar_age_for_risk(
                        "news"
                    )
                body = news_status_payload(
                    payload,
                    self.risk_server.state_store,
                    cached_events,
                    cached_news_age,
                    self.risk_server.config,
                )
            elif path == "/v1/market-status":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                with self.risk_server.market_lock:
                    cached_closures = list(
                        self.risk_server.market_closures
                    )
                    cached_market_age = (
                        self.risk_server.calendar_age_for_risk("market")
                    )
                body = market_status_payload(
                    payload,
                    self.risk_server.state_store,
                    cached_closures,
                    cached_market_age,
                    self.risk_server.config,
                )
            elif path == "/v1/closed-trade-sync":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                body = closed_trade_sync_payload(
                    payload=payload,
                    state_store=self.risk_server.state_store,
                    request_id=request_id,
                )
            elif path == "/v1/qualification-history-sync":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                body = qualification_history_sync_payload(
                    payload=payload,
                    state_store=self.risk_server.state_store,
                )
            elif path == "/v1/trading-day-sync":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                body = trading_day_sync_payload(
                    payload=payload,
                    state_store=self.risk_server.state_store,
                    request_id=request_id,
                )
            elif path == "/v1/news-sync":
                events = _news_events(_required(payload, "events"))
                coverage_start, coverage_end = _calendar_coverage(payload)
                _validate_news_coverage(
                    events,
                    coverage_start,
                    coverage_end,
                )
                fetched_at = _timestamp(
                    _required(payload, "fetched_at"),
                    "fetched_at",
                )
                age_seconds = (
                    datetime.now(timezone.utc)
                    - fetched_at.astimezone(timezone.utc)
                ).total_seconds()
                if age_seconds < -30:
                    raise RequestError(
                        "fetched_at is more than 30 seconds in the future"
                    )
                age = max(0, int(age_seconds))
                with self.risk_server.news_lock:
                    incoming_at = fetched_at.astimezone(timezone.utc)
                    cached_at = (
                        self.risk_server.news_fetched_at.astimezone(
                            timezone.utc
                        )
                        if self.risk_server.news_fetched_at is not None
                        else None
                    )
                    if (
                        cached_at is not None
                        and incoming_at < cached_at
                    ):
                        raise RequestError(
                            "news calendar update is older than the cached update"
                        )
                    if (
                        cached_at is not None
                        and incoming_at == cached_at
                        and (
                            events != self.risk_server.news_events
                            or coverage_start
                            != self.risk_server.news_coverage_start
                            or coverage_end
                            != self.risk_server.news_coverage_end
                        )
                    ):
                        raise RequestError(
                            "news calendar timestamp already has different content "
                            "or coverage"
                        )
                    if self.risk_server.state_store is not None:
                        self.risk_server.state_store.save_calendar_snapshot(
                            calendar_type="news",
                            fetched_at=fetched_at,
                            coverage_start=coverage_start,
                            coverage_end=coverage_end,
                            payload=_news_calendar_payload(events),
                            rule_version=self.risk_server.rule_version,
                        )
                    self.risk_server.news_events = events
                    self.risk_server.news_fetched_at = fetched_at
                    self.risk_server.news_coverage_start = coverage_start
                    self.risk_server.news_coverage_end = coverage_end
                    self.risk_server.news_calendar_rule_version = (
                        self.risk_server.rule_version
                    )
                    with self.risk_server.metrics_lock:
                        sync_metric_key = ("news", "success")
                        self.risk_server.calendar_sync_counts[
                            sync_metric_key
                        ] = (
                            self.risk_server.calendar_sync_counts.get(
                                sync_metric_key,
                                0,
                            )
                            + 1
                        )
                body = {
                    "ok": True,
                    "event_count": len(events),
                    "news_data_age_seconds": age,
                    "coverage_start": coverage_start.isoformat(),
                    "coverage_end": coverage_end.isoformat(),
                }
            elif path == "/v1/market-sync":
                closures = _market_closures(
                    _required(payload, "closures")
                )
                coverage_start, coverage_end = _calendar_coverage(payload)
                _validate_market_coverage(
                    closures,
                    coverage_start,
                    coverage_end,
                )
                fetched_at = _timestamp(
                    _required(payload, "fetched_at"),
                    "fetched_at",
                )
                age_seconds = (
                    datetime.now(timezone.utc)
                    - fetched_at.astimezone(timezone.utc)
                ).total_seconds()
                if age_seconds < -30:
                    raise RequestError(
                        "fetched_at is more than 30 seconds in the future"
                    )
                age = max(0, int(age_seconds))
                with self.risk_server.market_lock:
                    incoming_at = fetched_at.astimezone(timezone.utc)
                    cached_at = (
                        self.risk_server.market_fetched_at.astimezone(
                            timezone.utc
                        )
                        if self.risk_server.market_fetched_at is not None
                        else None
                    )
                    if (
                        cached_at is not None
                        and incoming_at < cached_at
                    ):
                        raise RequestError(
                            "market calendar update is older than the cached update"
                        )
                    if (
                        cached_at is not None
                        and incoming_at == cached_at
                        and (
                            closures != self.risk_server.market_closures
                            or coverage_start
                            != self.risk_server.market_coverage_start
                            or coverage_end
                            != self.risk_server.market_coverage_end
                        )
                    ):
                        raise RequestError(
                            "market calendar timestamp already has different content "
                            "or coverage"
                        )
                    if self.risk_server.state_store is not None:
                        self.risk_server.state_store.save_calendar_snapshot(
                            calendar_type="market",
                            fetched_at=fetched_at,
                            coverage_start=coverage_start,
                            coverage_end=coverage_end,
                            payload=_market_calendar_payload(closures),
                            rule_version=self.risk_server.rule_version,
                        )
                    self.risk_server.market_closures = closures
                    self.risk_server.market_fetched_at = fetched_at
                    self.risk_server.market_coverage_start = coverage_start
                    self.risk_server.market_coverage_end = coverage_end
                    self.risk_server.market_calendar_rule_version = (
                        self.risk_server.rule_version
                    )
                    with self.risk_server.metrics_lock:
                        sync_metric_key = ("market", "success")
                        self.risk_server.calendar_sync_counts[
                            sync_metric_key
                        ] = (
                            self.risk_server.calendar_sync_counts.get(
                                sync_metric_key,
                                0,
                            )
                            + 1
                        )
                body = {
                    "ok": True,
                    "closure_count": len(closures),
                    "market_data_age_seconds": age,
                    "coverage_start": coverage_start.isoformat(),
                    "coverage_end": coverage_end.isoformat(),
                }
            elif re.fullmatch(
                r"/v1/admin/accounts/[^/]+/credentials",
                path,
            ):
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                account_id = _account_id(
                    path.split("/")[4]
                )
                body = create_credential_payload(
                    account_id=account_id,
                    payload=payload,
                    state_store=self.risk_server.state_store,
                    config_source=self.risk_server.config,
                )
            elif re.fullmatch(r"/v1/admin/credentials/[^/]+/rotate", path):
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                credential_id = path.split("/")[4]
                body = rotate_credential_payload(
                    credential_id=credential_id,
                    payload=payload,
                    state_store=self.risk_server.state_store,
                    config_source=self.risk_server.config,
                )
            elif re.fullmatch(r"/v1/admin/credentials/[^/]+/revoke", path):
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                credential_id = path.split("/")[4]
                body = revoke_credential_payload(
                    credential_id,
                    self.risk_server.state_store,
                )
            else:
                body = {"ok": False, "error": "not found"}
                self._audit(request_id, self.path, HTTPStatus.NOT_FOUND, body)
                self._send_json(HTTPStatus.NOT_FOUND, body)
                return
            body.setdefault("request_id", request_id)
            self._audit(request_id, self.path, HTTPStatus.OK, body)
            self._send_json(HTTPStatus.OK, body)
        except ForbiddenError as exc:
            self._calendar_sync_failure(path)
            body = {"ok": False, "error": str(exc), "request_id": request_id}
            self._audit(request_id, self.path, HTTPStatus.FORBIDDEN, body)
            self._send_json(HTTPStatus.FORBIDDEN, body)
        except AuthorizationError as exc:
            self._calendar_sync_failure(path)
            body = {"ok": False, "error": str(exc), "request_id": request_id}
            self._audit(request_id, self.path, HTTPStatus.UNAUTHORIZED, body)
            self._send_json(HTTPStatus.UNAUTHORIZED, body)
        except EndpointDisabledError as exc:
            self._calendar_sync_failure(path)
            body = {"ok": False, "error": str(exc), "request_id": request_id}
            self._audit(request_id, self.path, HTTPStatus.FORBIDDEN, body)
            self._send_json(HTTPStatus.FORBIDDEN, body)
        except (RequestError, KeyError, ValueError) as exc:
            self._calendar_sync_failure(path)
            body = {"ok": False, "error": str(exc), "request_id": request_id}
            self._audit(request_id, self.path, HTTPStatus.BAD_REQUEST, body)
            self._send_json(HTTPStatus.BAD_REQUEST, body)
        except Exception:
            self._calendar_sync_failure(path)
            # Do not leak stack traces or local paths to a platform adapter.
            LOGGER.exception(
                "Unhandled risk service error request_id=%s endpoint=%s",
                request_id,
                self.path,
            )
            body = {
                "ok": False,
                "error": "internal risk service error",
                "request_id": request_id,
            }
            self._audit(
                request_id,
                self.path,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                body,
            )
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, body)


class RiskHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        config_path: str | Path,
        auth_token: str = "",
        max_body_bytes: int = 1_000_000,
        state_path: str | Path | None = None,
        allow_stateless_evaluate: bool = False,
        allow_stateless_position_size: bool = False,
        allow_remote_bind: bool = False,
        read_timeout_seconds: float = 5.0,
        tls_cert_path: str | Path | None = None,
        tls_key_path: str | Path | None = None,
        tls_ca_path: str | Path | None = None,
        require_client_cert: bool = False,
        require_account_credentials: bool | None = None,
    ):
        self.config_path = str(config_path)
        self.auth_token = auth_token
        self.max_body_bytes = max_body_bytes
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.allow_stateless_evaluate = allow_stateless_evaluate
        self.allow_stateless_position_size = allow_stateless_position_size
        self.read_timeout_seconds = read_timeout_seconds
        if read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")
        loopback_bind = _is_loopback_host(server_address[0])
        if not loopback_bind and not allow_remote_bind:
            raise ValueError(
                "non-loopback bind requires explicit allow_remote_bind=True; "
                "prefer a loopback listener behind an authenticated TLS proxy"
            )
        self.require_auth = bool(auth_token) or not loopback_bind
        if self.require_auth and not auth_token:
            raise ValueError(
                "auth_token is required when the API is not bound to loopback"
            )
        self.news_lock = threading.RLock()
        self.market_lock = threading.RLock()
        self.audit_lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.rate_by_client: dict[str, list[float]] = {}
        self.max_requests_per_minute = 1200
        self.http_requests: dict[tuple[str, str, str], int] = {}
        self.decision_counts: dict[str, int] = {}
        self.calendar_sync_counts: dict[tuple[str, str], int] = {}
        self.calendar_restore_counts: dict[tuple[str, str], int] = {}
        self.news_events: list[NewsEvent] = []
        self.news_fetched_at: datetime | None = None
        self.news_coverage_start: datetime | None = None
        self.news_coverage_end: datetime | None = None
        self.news_calendar_rule_version: str | None = None
        self.market_closures: list[MarketClosure] = []
        self.market_fetched_at: datetime | None = None
        self.market_coverage_start: datetime | None = None
        self.market_coverage_end: datetime | None = None
        self.market_calendar_rule_version: str | None = None
        self.audit_path = os.environ.get("RISK_AUDIT_PATH", "")
        self.qualification_dashboard_path = (
            Path(__file__).resolve().parents[1]
            / "dashboard"
            / "qualification.html"
        )
        config = dict(_load_config(config_path))
        validate_config(config)
        self.config = config
        self.rule_version = str(config["rule_version"])
        self.day_timezone = str(
            config.get("ftmo_day_timezone", "Europe/Prague")
        )
        self.state_store: StateStore | None = None
        security = config.get("security", {})
        self.account_credentials_required = (
            bool(security.get("require_account_credentials", True))
            if require_account_credentials is None
            else require_account_credentials
        )
        config_requires_mtls = bool(security.get("require_mtls", False))
        self.require_client_cert = bool(
            require_client_cert or config_requires_mtls
        )
        if self.require_client_cert and tls_ca_path is None:
            raise ValueError(
                "tls_ca_path is required when client certificate validation "
                "is enabled"
            )
        if (tls_cert_path is None) != (tls_key_path is None):
            raise ValueError(
                "tls_cert_path and tls_key_path must be supplied together"
            )
        if self.require_client_cert and tls_cert_path is None:
            raise ValueError(
                "client certificate validation requires TLS certificate and key"
            )
        self.mtls_enabled = tls_cert_path is not None
        self._state_server_lock_path: Path | None = None
        try:
            if state_path:
                # Lock before opening SQLite so a competing process cannot
                # touch the state file while it is failing startup.
                self._state_server_lock_path = _acquire_state_server_lock(
                    state_path
                )
                self.state_store = StateStore(
                    state_path,
                    day_timezone=self.day_timezone,
                )
            super().__init__(server_address, RiskRequestHandler)
            if self.mtls_enabled:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(
                    certfile=str(tls_cert_path),
                    keyfile=str(tls_key_path),
                )
                if tls_ca_path is not None:
                    context.load_verify_locations(cafile=str(tls_ca_path))
                context.verify_mode = (
                    ssl.CERT_REQUIRED
                    if self.require_client_cert
                    else ssl.CERT_NONE
                )
                self.socket = context.wrap_socket(
                    self.socket,
                    server_side=True,
                )
            elif self.require_client_cert:
                raise ValueError(
                    "client certificate validation requires TLS certificate "
                    "and key"
                )
            self._restore_calendars()
        except Exception:
            try:
                super().server_close()
            except Exception:
                pass
            if self.state_store is not None:
                self.state_store.close()
            self._release_state_server_lock()
            raise

    def _release_state_server_lock(self) -> None:
        if self._state_server_lock_path is None:
            return
        try:
            self._state_server_lock_path.unlink(missing_ok=True)
        finally:
            self._state_server_lock_path = None

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            if self.state_store is not None:
                self.state_store.close()
            self._release_state_server_lock()

    def news_age_seconds(self) -> int | None:
        if self.news_fetched_at is None:
            return None
        return max(
            0,
            int(
                (
                    datetime.now(timezone.utc)
                    - self.news_fetched_at.astimezone(timezone.utc)
                ).total_seconds()
            ),
        )

    def market_age_seconds(self) -> int | None:
        if self.market_fetched_at is None:
            return None
        return max(
            0,
            int(
                (
                    datetime.now(timezone.utc)
                    - self.market_fetched_at.astimezone(timezone.utc)
                ).total_seconds()
            ),
        )

    def _restore_calendars(self) -> None:
        if self.state_store is None:
            return
        try:
            snapshots = self.state_store.get_calendar_snapshots()
        except Exception:
            LOGGER.exception("failed to read persisted calendars")
            with self.metrics_lock:
                for calendar_type in ("news", "market"):
                    key = (calendar_type, "failure")
                    self.calendar_restore_counts[key] = (
                        self.calendar_restore_counts.get(key, 0) + 1
                    )
            return

        for calendar_type, snapshot in snapshots.items():
            if calendar_type not in {"news", "market"}:
                LOGGER.error(
                    "unsupported persisted calendar type: %s",
                    calendar_type,
                )
                continue
            if snapshot.rule_version != self.rule_version:
                LOGGER.error(
                    "refusing persisted %s calendar from rule version %s; "
                    "current rule version is %s",
                    calendar_type,
                    snapshot.rule_version,
                    self.rule_version,
                )
                with self.metrics_lock:
                    key = (calendar_type, "rule_mismatch")
                    self.calendar_restore_counts[key] = (
                        self.calendar_restore_counts.get(key, 0) + 1
                    )
                continue
            try:
                if calendar_type == "news":
                    events = _news_events(
                        cast(list[Mapping[str, Any]], snapshot.payload)
                    )
                    if (
                        snapshot.coverage_start is not None
                        and snapshot.coverage_end is not None
                    ):
                        _validate_news_coverage(
                            events,
                            snapshot.coverage_start,
                            snapshot.coverage_end,
                        )
                    with self.news_lock:
                        self.news_events = events
                        self.news_fetched_at = snapshot.fetched_at
                        self.news_coverage_start = snapshot.coverage_start
                        self.news_coverage_end = snapshot.coverage_end
                        self.news_calendar_rule_version = snapshot.rule_version
                else:
                    closures = _market_closures(
                        cast(list[Mapping[str, Any]], snapshot.payload)
                    )
                    if (
                        snapshot.coverage_start is not None
                        and snapshot.coverage_end is not None
                    ):
                        _validate_market_coverage(
                            closures,
                            snapshot.coverage_start,
                            snapshot.coverage_end,
                        )
                    with self.market_lock:
                        self.market_closures = closures
                        self.market_fetched_at = snapshot.fetched_at
                        self.market_coverage_start = snapshot.coverage_start
                        self.market_coverage_end = snapshot.coverage_end
                        self.market_calendar_rule_version = snapshot.rule_version
            except Exception:
                LOGGER.exception(
                    "failed to restore persisted %s calendar",
                    calendar_type,
                )
                with self.metrics_lock:
                    key = (calendar_type, "failure")
                    self.calendar_restore_counts[key] = (
                        self.calendar_restore_counts.get(key, 0) + 1
                    )
                continue
            with self.metrics_lock:
                key = (calendar_type, "success")
                self.calendar_restore_counts[key] = (
                    self.calendar_restore_counts.get(key, 0) + 1
                )

    def calendar_health(self, calendar_type: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if calendar_type == "news":
            with self.news_lock:
                fetched_at = self.news_fetched_at
                coverage_start = self.news_coverage_start
                coverage_end = self.news_coverage_end
                rule_version = self.news_calendar_rule_version
            age = self.news_age_seconds()
            max_age_seconds = int(
                self.config["news_controls"]["max_calendar_age_seconds"]
            )
            required_start = now - timedelta(
                minutes=int(
                    self.config["news_controls"]["internal_after_minutes"]
                )
            )
            required_end = now + timedelta(
                minutes=int(
                    self.config["news_controls"]["internal_before_minutes"]
                )
            )
        else:
            with self.market_lock:
                fetched_at = self.market_fetched_at
                coverage_start = self.market_coverage_start
                coverage_end = self.market_coverage_end
                rule_version = self.market_calendar_rule_version
            age = self.market_age_seconds()
            max_age_seconds = int(
                self.config["market_close_controls"][
                    "max_schedule_age_seconds"
                ]
            )
            required_start = now
            required_end = now + timedelta(
                minutes=int(
                    self.config["market_close_controls"][
                        "gap_open_block_before_minutes"
                    ]
                )
            )
        rule_version_match = (
            rule_version == self.rule_version
            if rule_version is not None
            else False
        )
        coverage_sufficient = bool(
            coverage_start is not None
            and coverage_end is not None
            and coverage_start <= required_start
            and coverage_end >= required_end
        )
        stale = (
            fetched_at is None
            or age is None
            or age > max_age_seconds
            or not rule_version_match
            or not coverage_sufficient
        )
        return {
            "present": fetched_at is not None,
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "age_seconds": age,
            "max_age_seconds": max_age_seconds,
            "stale": stale,
            "persistent": self.state_store is not None,
            "rule_version": rule_version,
            "rule_version_match": rule_version_match,
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
            "required_start": required_start.isoformat(),
            "required_end": required_end.isoformat(),
            "coverage_sufficient": coverage_sufficient,
        }

    def calendar_age_for_risk(self, calendar_type: str) -> int | None:
        health = self.calendar_health(calendar_type)
        return None if health["stale"] else cast(int, health["age_seconds"])

    def readiness(self) -> dict[str, Any]:
        database_up = bool(
            self.state_store is not None
            and self.state_store.database_healthy()
        )
        news = self.calendar_health("news")
        market = self.calendar_health("market")
        reasons: list[str] = []
        if not database_up:
            reasons.append("persistent state database is unavailable")
        if news["stale"]:
            reasons.append("news calendar is missing, stale, or under-covered")
        if market["stale"]:
            reasons.append(
                "market closure calendar is missing, stale, or under-covered"
            )
        return {
            "ready": not reasons,
            "database_up": database_up,
            "news_calendar_ready": not news["stale"],
            "market_calendar_ready": not market["stale"],
            "reasons": reasons,
        }

    def observe_response(
        self,
        method: str,
        path: str,
        status: HTTPStatus,
        body: Mapping[str, Any],
    ) -> None:
        endpoint = _metric_endpoint(path)
        status_text = str(int(status))
        decision = body.get("decision")
        decision_code = (
            str(decision.get("code"))
            if isinstance(decision, Mapping) and decision.get("code")
            else None
        )
        with self.metrics_lock:
            key = (method.upper(), endpoint, status_text)
            self.http_requests[key] = self.http_requests.get(key, 0) + 1
            if decision_code is not None:
                self.decision_counts[decision_code] = (
                    self.decision_counts.get(decision_code, 0) + 1
                )

    def observe_calendar_sync(self, calendar: str, result: str) -> None:
        with self.metrics_lock:
            key = (calendar, result)
            self.calendar_sync_counts[key] = (
                self.calendar_sync_counts.get(key, 0) + 1
            )

    def _metric_lines(self) -> list[str]:
        lines = [
            "# HELP ftmo_risk_http_requests_total HTTP responses by endpoint.",
            "# TYPE ftmo_risk_http_requests_total counter",
        ]
        with self.metrics_lock:
            requests = dict(self.http_requests)
            decisions = dict(self.decision_counts)
            calendar_sync = dict(self.calendar_sync_counts)
            calendar_restore = dict(self.calendar_restore_counts)
        for (method, endpoint, status), count in sorted(requests.items()):
            lines.append(
                "ftmo_risk_http_requests_total"
                f'{{method="{_prometheus_label(method)}",'
                f'endpoint="{_prometheus_label(endpoint)}",'
                f'status="{_prometheus_label(status)}"}} '
                f"{count}"
            )
        lines.extend(
            [
                "# HELP ftmo_risk_decisions_total Risk decisions by code.",
                "# TYPE ftmo_risk_decisions_total counter",
            ]
        )
        for code, count in sorted(decisions.items()):
            lines.append(
                "ftmo_risk_decisions_total"
                f'{{code="{_prometheus_label(code)}"}} {count}'
            )
        lines.extend(
            [
                "# HELP ftmo_risk_calendar_sync_total Calendar sync results.",
                "# TYPE ftmo_risk_calendar_sync_total counter",
            ]
        )
        for (calendar, result), count in sorted(calendar_sync.items()):
            lines.append(
                "ftmo_risk_calendar_sync_total"
                f'{{calendar="{_prometheus_label(calendar)}",'
                f'result="{_prometheus_label(result)}"}} {count}'
            )
        lines.extend(
            [
                "# HELP ftmo_risk_calendar_restore_total Calendar restore results.",
                "# TYPE ftmo_risk_calendar_restore_total counter",
            ]
        )
        for (calendar, result), count in sorted(calendar_restore.items()):
            lines.append(
                "ftmo_risk_calendar_restore_total"
                f'{{calendar="{_prometheus_label(calendar)}",'
                f'result="{_prometheus_label(result)}"}} {count}'
            )
        lines.extend(
            [
                "# HELP ftmo_risk_calendar_present Whether a calendar is loaded.",
                "# TYPE ftmo_risk_calendar_present gauge",
                "# HELP ftmo_risk_calendar_age_seconds Calendar age.",
                "# TYPE ftmo_risk_calendar_age_seconds gauge",
                "# HELP ftmo_risk_calendar_stale Whether a calendar is missing, stale, or uses an inactive rule version.",
                "# TYPE ftmo_risk_calendar_stale gauge",
                "# HELP ftmo_risk_calendar_rule_version_match Whether the loaded calendar uses the active rule version.",
                "# TYPE ftmo_risk_calendar_rule_version_match gauge",
                "# HELP ftmo_risk_calendar_coverage_sufficient Whether the calendar covers the full active guard horizon.",
                "# TYPE ftmo_risk_calendar_coverage_sufficient gauge",
            ]
        )
        for calendar in ("news", "market"):
            health = self.calendar_health(calendar)
            present = 1 if health["present"] else 0
            age = health["age_seconds"]
            lines.append(
                "ftmo_risk_calendar_present"
                f'{{calendar="{_prometheus_label(calendar)}"}} '
                f"{present}"
            )
            lines.append(
                "ftmo_risk_calendar_age_seconds"
                f'{{calendar="{_prometheus_label(calendar)}"}} '
                f"{age if age is not None else -1}"
            )
            lines.append(
                "ftmo_risk_calendar_stale"
                f'{{calendar="{_prometheus_label(calendar)}"}} '
                f"{1 if health['stale'] else 0}"
            )
            lines.append(
                "ftmo_risk_calendar_rule_version_match"
                f'{{calendar="{_prometheus_label(calendar)}"}} '
                f"{1 if health['rule_version_match'] else 0}"
            )
            lines.append(
                "ftmo_risk_calendar_coverage_sufficient"
                f'{{calendar="{_prometheus_label(calendar)}"}} '
                f"{1 if health['coverage_sufficient'] else 0}"
            )

        readiness = self.readiness()
        lines.extend(
            [
                "# HELP ftmo_risk_ready_for_risk_increase Whether persistent state and both calendars are ready for new risk.",
                "# TYPE ftmo_risk_ready_for_risk_increase gauge",
                "ftmo_risk_ready_for_risk_increase "
                f"{1 if readiness['ready'] else 0}",
            ]
        )

        lines.extend(
            [
                "# HELP ftmo_risk_account_status Account status counts.",
                "# TYPE ftmo_risk_account_status gauge",
            ]
        )
        database_up = (
            1
            if (
                self.state_store is not None
                and self.state_store.database_healthy()
            )
            else 0
        )
        accounts: list[StoredAccount] = []
        unknown = 0
        backup: dict[str, Any] = {}
        credential_metrics = {
            "active": 0,
            "expired": 0,
            "expiring_soon": 0,
            "not_yet_active": 0,
        }
        if database_up and self.state_store is not None:
            try:
                accounts = self.state_store.all_accounts()
                unknown = self.state_store.unknown_execution_count()
                backup = self.state_store.backup_metrics()
                credential_metrics = self.state_store.credential_metrics()
            except Exception:
                LOGGER.exception("failed to collect SQLite metrics")
                database_up = 0
        status_counts = {
            "GREEN": 0,
            "AMBER": 0,
            "RED": 0,
            "LOCKED": 0,
            "BREACH": 0,
            "UNKNOWN": 0,
        }
        uncertain_accounts = 0
        stale_qualification_snapshots = 0
        qualification_age_limit = int(
            self.config["qualification_controls"][
                "max_account_snapshot_age_seconds"
            ]
        )
        for account in accounts:
            if account.snapshot.data_uncertain:
                uncertain_accounts += 1
            if account.snapshot.data_age_seconds > qualification_age_limit:
                stale_qualification_snapshots += 1
            try:
                profile, _ = _profile(
                    self.config,
                    {
                        "account_type": account.account_type.value,
                        "phase": account.phase.value,
                        "style": account.style.value,
                    },
                )
                status = RiskEngine(
                    profile,
                    day_timezone=self.day_timezone,
                ).status(account.snapshot)
                status_counts[status] = status_counts.get(status, 0) + 1
            except Exception:
                LOGGER.exception(
                    "failed to calculate account metrics account_id=%s",
                    account.account_id,
                )
                status_counts["UNKNOWN"] += 1
        for status, count in sorted(status_counts.items()):
            lines.append(
                "ftmo_risk_account_status"
                f'{{status="{_prometheus_label(status)}"}} {count}'
            )
        lines.extend(
            [
                "# HELP ftmo_risk_accounts_uncertain Accounts with uncertain state.",
                "# TYPE ftmo_risk_accounts_uncertain gauge",
                f"ftmo_risk_accounts_uncertain {uncertain_accounts}",
                "# HELP ftmo_risk_qualification_snapshots_stale Accounts whose qualification snapshot is too old.",
                "# TYPE ftmo_risk_qualification_snapshots_stale gauge",
                f"ftmo_risk_qualification_snapshots_stale {stale_qualification_snapshots}",
                "# HELP ftmo_risk_unknown_execution_records Unknown executions.",
                "# TYPE ftmo_risk_unknown_execution_records gauge",
                f"ftmo_risk_unknown_execution_records {unknown}",
            ]
        )
        lines.extend(
            [
                "# HELP ftmo_risk_database_up SQLite health status.",
                "# TYPE ftmo_risk_database_up gauge",
                f"ftmo_risk_database_up {database_up}",
                "# HELP ftmo_risk_credentials_expiring_soon Active credentials expiring within 24 hours.",
                "# TYPE ftmo_risk_credentials_expiring_soon gauge",
                f"ftmo_risk_credentials_expiring_soon {credential_metrics['expiring_soon']}",
                "# HELP ftmo_risk_credentials_expired Active credentials past expiry.",
                "# TYPE ftmo_risk_credentials_expired gauge",
                f"ftmo_risk_credentials_expired {credential_metrics['expired']}",
            ]
        )
        lines.extend(
            [
                "# HELP ftmo_risk_backup_runs_total Backup and restore outcomes.",
                "# TYPE ftmo_risk_backup_runs_total counter",
            ]
        )
        for operation in ("backup", "restore"):
            for result in ("success", "failure"):
                key = f"{operation}_{result}"
                lines.append(
                    "ftmo_risk_backup_runs_total"
                    f'{{operation="{operation}",result="{result}"}} '
                    f"{backup.get(key, 0)}"
                )
        latest = backup.get("last_backup_at")
        latest_age = -1
        if latest:
            latest_age = max(
                0,
                int(
                    (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(latest).astimezone(
                            timezone.utc
                        )
                    ).total_seconds()
                ),
            )
        lines.extend(
            [
                "# HELP ftmo_risk_last_backup_age_seconds Age of last backup.",
                "# TYPE ftmo_risk_last_backup_age_seconds gauge",
                f"ftmo_risk_last_backup_age_seconds {latest_age}",
            ]
        )
        return lines

    def metrics_text(self) -> str:
        return "\n".join(self._metric_lines()) + "\n"

    def write_audit(self, record: Mapping[str, Any]) -> None:
        if not self.audit_path:
            return
        path = Path(self.audit_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            _jsonable(dict(record)),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        with self.audit_lock:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(path, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def allow_request(self, client: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        with self.rate_lock:
            timestamps = self.rate_by_client.setdefault(client, [])
            timestamps[:] = [
                timestamp for timestamp in timestamps if timestamp >= cutoff
            ]
            if len(timestamps) >= self.max_requests_per_minute:
                return False
            timestamps.append(now)
            if len(self.rate_by_client) > 1000:
                self.rate_by_client = {
                    key: values
                    for key, values in self.rate_by_client.items()
                    if values and values[-1] >= cutoff
                }
            return True


def make_server(
    host: str,
    port: int,
    config_path: str | Path,
    auth_token: str = "",
    state_path: str | Path | None = None,
    allow_stateless_evaluate: bool = False,
    allow_stateless_position_size: bool = False,
    allow_remote_bind: bool = False,
    read_timeout_seconds: float = 5.0,
    tls_cert_path: str | Path | None = None,
    tls_key_path: str | Path | None = None,
    tls_ca_path: str | Path | None = None,
    require_client_cert: bool = False,
    require_account_credentials: bool | None = None,
) -> RiskHTTPServer:
    return RiskHTTPServer(
        (host, port),
        config_path,
        auth_token=auth_token,
        state_path=state_path,
        allow_stateless_evaluate=allow_stateless_evaluate,
        allow_stateless_position_size=allow_stateless_position_size,
        allow_remote_bind=allow_remote_bind,
        read_timeout_seconds=read_timeout_seconds,
        tls_cert_path=tls_cert_path,
        tls_key_path=tls_key_path,
        tls_ca_path=tls_ca_path,
        require_client_cert=require_client_cert,
        require_account_credentials=require_account_credentials,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local FTMO risk API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "config" / "ftmo-v2.json"),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("RISK_API_TOKEN", ""),
        help="Required X-Risk-Token value; prefer RISK_API_TOKEN.",
    )
    parser.add_argument(
        "--state",
        default=os.environ.get("RISK_STATE_PATH", "runtime/risk-state.db"),
        help="SQLite path for account and frequency state.",
    )
    parser.add_argument(
        "--allow-remote-bind",
        action="store_true",
        help=(
            "Allow a non-loopback listener. Use only behind an authenticated "
            "TLS proxy or equivalent private transport."
        ),
    )
    parser.add_argument(
        "--allow-stateless-evaluate",
        action="store_true",
        help="Enable stateless evaluation for an isolated test/replay instance.",
    )
    parser.add_argument(
        "--allow-stateless-position-size",
        action="store_true",
        help=(
            "Enable stateless position sizing for an isolated test/replay "
            "instance."
        ),
    )
    parser.add_argument(
        "--tls-cert",
        default=os.environ.get("RISK_TLS_CERT", ""),
        help="Server TLS certificate path.",
    )
    parser.add_argument(
        "--tls-key",
        default=os.environ.get("RISK_TLS_KEY", ""),
        help="Server TLS private key path.",
    )
    parser.add_argument(
        "--tls-ca",
        default=os.environ.get("RISK_TLS_CA", ""),
        help="CA bundle for mTLS client certificate validation.",
    )
    parser.add_argument(
        "--require-client-cert",
        action="store_true",
        help="Require and validate a client certificate using --tls-ca.",
    )
    args = parser.parse_args()
    if not args.token:
        parser.error(
            "--token or RISK_API_TOKEN is required; refusing to start "
            "without API authentication"
        )
    server = make_server(
        args.host,
        args.port,
        args.config,
        args.token,
        state_path=args.state,
        allow_stateless_evaluate=args.allow_stateless_evaluate,
        allow_stateless_position_size=args.allow_stateless_position_size,
        allow_remote_bind=args.allow_remote_bind,
        tls_cert_path=args.tls_cert or None,
        tls_key_path=args.tls_key or None,
        tls_ca_path=args.tls_ca or None,
        require_client_cert=args.require_client_cert,
    )
    print(
        f"FTMO risk API listening on "
        f"{'https' if server.mtls_enabled else 'http'}://"
        f"{args.host}:{args.port} "
        f"(rule {server.rule_version})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
