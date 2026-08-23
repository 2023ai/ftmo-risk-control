"""Independent qualification calculations for the progress dashboard.

The risk gate and qualification dashboard intentionally remain separate:
an ``ALLOW`` decision only means that one requested action fits the configured
risk controls. It does not prove that the account has met every trading
objective.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .state_store import StoredAccount


ZERO = Decimal("0")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _phase_controls(
    config: Mapping[str, Any],
    account: StoredAccount,
) -> Mapping[str, Any]:
    controls = config["qualification_controls"]
    account_controls = controls[account.account_type.value]
    phase_controls = account_controls.get(account.phase.value)
    if not isinstance(phase_controls, Mapping):
        raise ValueError(
            "qualification controls are not configured for "
            f"{account.account_type.value}/{account.phase.value}"
        )
    return phase_controls


def qualification_snapshot(
    *,
    config: Mapping[str, Any],
    account: StoredAccount,
    closed_trades: Sequence[Mapping[str, Any]],
    trading_day_events: Sequence[Mapping[str, Any]],
    history_status: Mapping[str, Any] | None,
) -> dict[str, Any]:
    account_controls = _phase_controls(
        config,
        account,
    )
    daily_profit: defaultdict[str, Decimal] = defaultdict(lambda: ZERO)
    for trade in closed_trades:
        day = str(trade["ftmo_day"])
        daily_profit[day] += _decimal(
            trade["net_profit"],
            "closed trade net_profit",
        )

    positive_days = {
        day: profit for day, profit in daily_profit.items() if profit > ZERO
    }
    total_profit = sum(daily_profit.values(), ZERO)
    positive_days_profit = sum(positive_days.values(), ZERO)
    best_day_profit = max(positive_days.values(), default=ZERO)
    if positive_days_profit > ZERO:
        best_day_ratio = best_day_profit / positive_days_profit
    else:
        best_day_ratio = ZERO

    raw_best_day_limit = account_controls.get("best_day_rule_pct")
    best_day_limit = (
        _decimal(raw_best_day_limit, "best_day_rule_pct")
        if raw_best_day_limit is not None
        else None
    )
    best_day_applicable = best_day_limit is not None
    best_day_compliant = (
        True
        if best_day_limit is None
        else (
            positive_days_profit > ZERO
            and best_day_ratio <= best_day_limit
        )
    )
    trading_day_keys = sorted(
        {str(value["ftmo_day"]) for value in trading_day_events}
    )
    trading_days = len(trading_day_keys)
    minimum_trading_days = int(
        account_controls["minimum_trading_days"]
    )
    raw_target_pct = account_controls.get("profit_target_pct")
    target_pct = (
        _decimal(raw_target_pct, "profit_target_pct")
        if raw_target_pct is not None
        else None
    )
    target_amount = (
        account.snapshot.initial_capital * target_pct
        if target_pct is not None
        else None
    )
    progress_ratio = (
        total_profit / target_amount
        if target_amount is not None and target_amount > ZERO
        else ZERO
    )
    profit_target_applicable = target_amount is not None
    profit_target_met = (
        total_profit >= target_amount
        if target_amount is not None
        else True
    )
    minimum_days_applicable = minimum_trading_days > 0
    minimum_days_met = trading_days >= minimum_trading_days
    uncertainty_reasons: list[str] = []
    if account.snapshot.data_uncertain:
        uncertainty_reasons.append("account settlement baseline is uncertain")
    history_complete = history_status is not None
    cycle_id: str | None = None
    history_start_at: str | None = None
    complete_through: str | None = None
    if history_status is None:
        uncertainty_reasons.append(
            "qualification history completeness has not been confirmed"
        )
    else:
        cycle_id = str(history_status["cycle_id"])
        history_start_at = str(history_status["history_start_at"])
        complete_through = str(history_status["complete_through"])
        if history_status["phase"] != account.phase.value:
            history_complete = False
            uncertainty_reasons.append(
                "qualification history phase does not match the account"
            )
        complete_at = datetime.fromisoformat(complete_through)
        start_at = datetime.fromisoformat(history_start_at)
        snapshot_at = account.snapshot.as_of.astimezone(complete_at.tzinfo)
        if complete_at < snapshot_at:
            history_complete = False
            uncertainty_reasons.append(
                "closed-trade history does not cover the latest account snapshot"
            )
        for trade in closed_trades:
            if "closed_at" not in trade:
                continue
            trade_closed_at = datetime.fromisoformat(str(trade["closed_at"]))
            if trade_closed_at < start_at or trade_closed_at > complete_at:
                history_complete = False
                uncertainty_reasons.append(
                    "a synchronized trade falls outside the confirmed "
                    "history interval"
                )
                break
        for trading_day_event in trading_day_events:
            if "first_opened_at" not in trading_day_event:
                continue
            opened_at = datetime.fromisoformat(
                str(trading_day_event["first_opened_at"])
            )
            if opened_at < start_at or opened_at > complete_at:
                history_complete = False
                uncertainty_reasons.append(
                    "a trading-day event falls outside the confirmed "
                    "history interval"
                )
                break
    data_uncertain = bool(uncertainty_reasons)
    applicable_count = sum(
        (
            int(profit_target_applicable),
            int(minimum_days_applicable),
            int(best_day_applicable),
        )
    )
    eligible = (
        applicable_count > 0
        and not data_uncertain
        and profit_target_met
        and minimum_days_met
        and best_day_compliant
    )

    return {
        "ok": True,
        "account_id": account.account_id,
        "account_type": account.account_type.value,
        "phase": account.phase.value,
        "style": account.style.value,
        "rule_version": str(config["rule_version"]),
        "data_uncertain": data_uncertain,
        "uncertainty_reasons": uncertainty_reasons,
        "history": {
            "complete": history_complete,
            "phase": (
                str(history_status["phase"])
                if history_status is not None
                else None
            ),
            "cycle_id": cycle_id,
            "history_start_at": history_start_at,
            "complete_through": complete_through,
            "source": (
                str(history_status["source"])
                if history_status is not None
                else None
            ),
        },
        "closed_trade_count": len(closed_trades),
        "closed_trading_days": sorted(daily_profit),
        "profit_target": {
            "applicable": profit_target_applicable,
            "target_pct": (
                _decimal_text(target_pct)
                if target_pct is not None
                else None
            ),
            "target_amount": (
                _decimal_text(target_amount)
                if target_amount is not None
                else None
            ),
            "net_profit": _decimal_text(total_profit),
            "progress_ratio": (
                _decimal_text(progress_ratio)
                if profit_target_applicable
                else None
            ),
            "progress_pct": (
                _decimal_text(progress_ratio * Decimal("100"))
                if profit_target_applicable
                else None
            ),
            "met": profit_target_met,
        },
        "minimum_trading_days": {
            "applicable": minimum_days_applicable,
            "required": minimum_trading_days,
            "completed": trading_days,
            "met": minimum_days_met,
            "ftmo_days": trading_day_keys,
            "definition": "CE(S)T days with at least one opened position",
        },
        "best_day_rule": {
            "applicable": best_day_applicable,
            "limit_ratio": (
                _decimal_text(best_day_limit)
                if best_day_limit is not None
                else None
            ),
            "best_day_profit": _decimal_text(best_day_profit),
            "positive_days_profit": _decimal_text(positive_days_profit),
            "ratio": _decimal_text(best_day_ratio),
            "compliant": best_day_compliant,
        },
        "applicable_objective_count": applicable_count,
        "eligible": eligible,
        "qualification_status": (
            "not_applicable"
            if applicable_count == 0
            else "uncertain"
            if data_uncertain
            else "eligible"
            if eligible
            else "in_progress"
        ),
        "manual_review_required": data_uncertain,
        "qualification_scope": (
            "This is a progress calculation from synchronized closed trades; "
            "it is not a trading permission."
        ),
    }
