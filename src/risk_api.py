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
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import EnumMeta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
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
from .state_store import StateStore, StoredAccount


LOGGER = logging.getLogger(__name__)
ConfigSource = str | Path | Mapping[str, Any]


class RequestError(ValueError):
    """A client supplied an invalid risk request."""


class EndpointDisabledError(RequestError):
    """An endpoint is intentionally disabled in the current server mode."""


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
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"{field} must be a non-negative integer") from exc
    if result < 0:
        raise RequestError(f"{field} must be a non-negative integer")
    return result


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
        if request.stop_loss is not None and request.stop_loss <= 0:
            raise RequestError("opening stop loss must be positive")
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
        affected_symbols = item.get("affected_symbols", [])
        if (
            not isinstance(affected_symbols, list)
            or len(affected_symbols) > 100
        ):
            raise RequestError(
                "affected_symbols must be a list of at most 100 symbols"
            )
        events.append(
            NewsEvent(
                event_id=event_id,
                release_time=_timestamp(
                    _required(item, "release_time"),
                    "news.release_time",
                ),
                affected_symbols=frozenset(
                    str(symbol).strip()
                    for symbol in affected_symbols
                    if str(symbol).strip()
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
        affected_symbols = item.get("affected_symbols", [])
        if (
            not isinstance(affected_symbols, list)
            or len(affected_symbols) > 100
        ):
            raise RequestError(
                "affected_symbols must be a list of at most 100 symbols"
            )
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
                affected_symbols=frozenset(
                    str(symbol).strip()
                    for symbol in affected_symbols
                    if str(symbol).strip()
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
    stale = news_age_seconds is None or news_age_seconds > 60
    if stale:
        return {
            "ok": True,
            "rule_version": rule_version,
            "account_id": account_id,
            "symbol": symbol,
            "news_data_stale": True,
            "open_blocked": True,
            "force_flat": False,
            "cancel_pending": False,
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
            event_ids.append(event.event_id)
        if is_restricted_account and -hard_after <= delta <= hard_before:
            hard_window = True
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
            "cancel_pending": False,
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
            closure_ids.append(closure.closure_id)
        elif timedelta(0) <= until_start <= open_block_before:
            open_blocked = True
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
    closures = (
        _market_closures(payload["market_closures"])
        if "market_closures" in payload
        else list(default_market_closures or [])
    )
    market_age_seconds = payload.get(
        "market_data_age_seconds",
        default_market_age_seconds,
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
            or int(news_age_seconds) > 60
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
                or int(market_age_seconds) > max_market_age
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
    server_version = "FTMO-RiskAPI/1.0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.risk_server.read_timeout_seconds)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the service quiet by default; platform adapters log decisions.
        return

    @property
    def risk_server(self) -> "RiskHTTPServer":
        return self.server  # type: ignore[return-value]

    def _authorized(self) -> bool:
        expected = self.risk_server.auth_token
        if not self.risk_server.require_auth:
            return True
        supplied = self.headers.get("X-Risk-Token", "")
        return hmac.compare_digest(supplied, expected)

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
        self.end_headers()
        self.wfile.write(encoded)

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
                "endpoint": endpoint,
                "http_status": status,
                "client": self.client_address[0],
                "decision": body.get("decision"),
                "account": body.get("account"),
                "rule_version": body.get("rule_version"),
                "event_count": body.get("event_count"),
                "error": body.get("error"),
            }
        )

    def _read_json(self) -> Mapping[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestError("Content-Length must be an integer") from exc
        if length <= 0 or length > self.risk_server.max_body_bytes:
            raise RequestError("request body is empty or too large")
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise RequestError("request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise RequestError("request body must be a JSON object")
        return body

    def _request_id_from_headers(self) -> str:
        value = self.headers.get("X-Request-Id")
        return _request_id(value if value is not None else str(uuid4()))

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "invalid X-Risk-Token"},
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "ftmo-risk-api",
                "rule_version": self.risk_server.rule_version,
                "news_data_age_seconds": self.risk_server.news_age_seconds(),
                "market_data_age_seconds": (
                    self.risk_server.market_age_seconds()
                ),
                "persistent_state": self.risk_server.state_store is not None,
            },
        )

    def do_POST(self) -> None:
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
        if not self._authorized():
            body = {"ok": False, "error": "invalid X-Risk-Token"}
            self._audit(
                request_id,
                self.path,
                HTTPStatus.UNAUTHORIZED,
                body,
            )
            self._send_json(HTTPStatus.UNAUTHORIZED, body)
            return
        try:
            payload = self._read_json()
            if self.path == "/v1/evaluate":
                with self.risk_server.news_lock:
                    cached_events = list(self.risk_server.news_events)
                    cached_news_age = self.risk_server.news_age_seconds()
                with self.risk_server.market_lock:
                    cached_closures = list(
                        self.risk_server.market_closures
                    )
                    cached_market_age = (
                        self.risk_server.market_age_seconds()
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
            elif self.path == "/v1/position-size":
                if not self.risk_server.allow_stateless_position_size:
                    raise EndpointDisabledError(
                        "stateless /v1/position-size is disabled on this server"
                    )
                body = position_size_payload(
                    payload,
                    self.risk_server.config,
                )
            elif self.path == "/v1/account-sync":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                body = account_sync_payload(
                    payload,
                    self.risk_server.state_store,
                    self.risk_server.config,
                )
            elif self.path == "/v1/settlement-sync":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                body = settlement_sync_payload(
                    payload,
                    self.risk_server.state_store,
                )
            elif self.path == "/v1/execution-result":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                body = execution_result_payload(
                    payload,
                    self.risk_server.state_store,
                )
            elif self.path == "/v1/news-status":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                with self.risk_server.news_lock:
                    cached_events = list(self.risk_server.news_events)
                    cached_news_age = self.risk_server.news_age_seconds()
                body = news_status_payload(
                    payload,
                    self.risk_server.state_store,
                    cached_events,
                    cached_news_age,
                    self.risk_server.config,
                )
            elif self.path == "/v1/market-status":
                if self.risk_server.state_store is None:
                    raise RequestError(
                        "persistent state is disabled on this server"
                    )
                with self.risk_server.market_lock:
                    cached_closures = list(
                        self.risk_server.market_closures
                    )
                    cached_market_age = (
                        self.risk_server.market_age_seconds()
                    )
                body = market_status_payload(
                    payload,
                    self.risk_server.state_store,
                    cached_closures,
                    cached_market_age,
                    self.risk_server.config,
                )
            elif self.path == "/v1/news-sync":
                events = _news_events(_required(payload, "events"))
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
                        and events != self.risk_server.news_events
                    ):
                        raise RequestError(
                            "news calendar timestamp already has different content"
                        )
                    self.risk_server.news_events = events
                    self.risk_server.news_fetched_at = fetched_at
                body = {
                    "ok": True,
                    "event_count": len(events),
                    "news_data_age_seconds": age,
                }
            elif self.path == "/v1/market-sync":
                closures = _market_closures(
                    _required(payload, "closures")
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
                        and closures != self.risk_server.market_closures
                    ):
                        raise RequestError(
                            "market calendar timestamp already has different content"
                        )
                    self.risk_server.market_closures = closures
                    self.risk_server.market_fetched_at = fetched_at
                body = {
                    "ok": True,
                    "closure_count": len(closures),
                    "market_data_age_seconds": age,
                }
            else:
                body = {"ok": False, "error": "not found"}
                self._audit(request_id, self.path, HTTPStatus.NOT_FOUND, body)
                self._send_json(HTTPStatus.NOT_FOUND, body)
                return
            body.setdefault("request_id", request_id)
            self._audit(request_id, self.path, HTTPStatus.OK, body)
            self._send_json(HTTPStatus.OK, body)
        except EndpointDisabledError as exc:
            body = {"ok": False, "error": str(exc), "request_id": request_id}
            self._audit(request_id, self.path, HTTPStatus.FORBIDDEN, body)
            self._send_json(HTTPStatus.FORBIDDEN, body)
        except (RequestError, KeyError, ValueError) as exc:
            body = {"ok": False, "error": str(exc), "request_id": request_id}
            self._audit(request_id, self.path, HTTPStatus.BAD_REQUEST, body)
            self._send_json(HTTPStatus.BAD_REQUEST, body)
        except Exception:
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
        self.news_lock = threading.Lock()
        self.market_lock = threading.Lock()
        self.audit_lock = threading.Lock()
        self.rate_lock = threading.Lock()
        self.rate_by_client: dict[str, list[float]] = {}
        self.max_requests_per_minute = 1200
        self.news_events: list[NewsEvent] = []
        self.news_fetched_at: datetime | None = None
        self.market_closures: list[MarketClosure] = []
        self.market_fetched_at: datetime | None = None
        self.audit_path = os.environ.get("RISK_AUDIT_PATH", "")
        config = dict(_load_config(config_path))
        validate_config(config)
        self.config = config
        self.rule_version = str(config["rule_version"])
        self.day_timezone = str(
            config.get("ftmo_day_timezone", "Europe/Prague")
        )
        self.state_store = (
            StateStore(state_path, day_timezone=self.day_timezone)
            if state_path
            else None
        )
        super().__init__(server_address, RiskRequestHandler)

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
                    if values
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
    )
    print(
        f"FTMO risk API listening on http://{args.host}:{args.port} "
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
