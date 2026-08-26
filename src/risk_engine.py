from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo


ZERO = Decimal("0")


def _symbol_pattern_matches(pattern: str, symbol: str) -> bool:
    normalized_pattern = pattern.upper()
    normalized_symbol = symbol.upper()
    if normalized_pattern == "*":
        return True
    if normalized_pattern.endswith("*"):
        return normalized_symbol.startswith(normalized_pattern[:-1])
    return normalized_pattern == normalized_symbol


class AccountType(str, Enum):
    ONE_STEP = "one_step"
    TWO_STEP = "two_step"


class AccountPhase(str, Enum):
    EVALUATION = "evaluation"
    VERIFICATION = "verification"
    FTMO_ACCOUNT = "ftmo_account"


class AccountStyle(str, Enum):
    STANDARD = "standard"
    SWING = "swing"


class Action(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    MODIFY = "modify"
    CANCEL = "cancel"


class DecisionCode(str, Enum):
    ALLOW = "ALLOW"
    REJECT_OFFICIAL_BREACH = "REJECT_OFFICIAL_BREACH"
    REJECT_INTERNAL_LOCK = "REJECT_INTERNAL_LOCK"
    REJECT_NEWS = "REJECT_NEWS"
    REJECT_MARKET_CLOSE = "REJECT_MARKET_CLOSE"
    REJECT_STOP_LOSS = "REJECT_STOP_LOSS"
    REJECT_FREQUENCY = "REJECT_FREQUENCY"
    REJECT_RISK = "REJECT_RISK"
    REJECT_DATA_STALE = "REJECT_DATA_STALE"
    REJECT_UNKNOWN_EXECUTION = "REJECT_UNKNOWN_EXECUTION"


@dataclass(frozen=True)
class RuleProfile:
    account_type: AccountType
    phase: AccountPhase
    style: AccountStyle
    official_daily_loss_pct: Decimal
    official_max_loss_pct: Decimal
    max_loss_mode: str
    internal_daily_stop_pct: Decimal
    internal_max_loss_stop_pct: Decimal
    warning_utilization_pct: Decimal = Decimal("0.50")
    reduce_size_utilization_pct: Decimal = Decimal("0.70")
    lock_utilization_pct: Decimal = Decimal("0.80")
    single_trade_risk_pct: Decimal = Decimal("0.0025")
    max_open_risk_pct: Decimal = Decimal("0.010")
    daily_buffer_fraction_per_trade: Decimal = Decimal("0.20")
    max_loss_buffer_fraction_per_trade: Decimal = Decimal("0.10")
    news_hard_before_minutes: int = 2
    news_hard_after_minutes: int = 2
    news_internal_before_minutes: int = 10
    news_internal_after_minutes: int = 10
    news_force_flat_before_minutes: int = 10
    news_cancel_pending_before_minutes: int = 10
    news_max_calendar_age_seconds: int = 60
    max_opens_5m: int = 3
    max_opens_1h: int = 10
    max_opens_day: int = 30
    warning_requests_day: int = 500
    stop_requests_day: int = 1000
    min_modify_interval_seconds: int = 10
    market_flat_before_minutes: int = 10
    market_gap_open_block_before_minutes: int = 120
    market_restricted_break_min_minutes: int = 120

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        account_type: AccountType,
        phase: AccountPhase,
        style: AccountStyle,
    ) -> "RuleProfile":
        validate_config(config)
        account = config["accounts"][account_type.value]
        internal = config["internal_controls"]
        news = config["news_controls"]
        frequency = config["frequency_controls"]
        market_close = config.get("market_close_controls", {})
        if account_type == AccountType.ONE_STEP and style == AccountStyle.SWING:
            raise ValueError("Swing style is only available for 2-Step accounts")
        if (
            account_type == AccountType.ONE_STEP
            and phase == AccountPhase.VERIFICATION
        ):
            raise ValueError("Verification is only available for 2-Step accounts")
        return cls(
            account_type=account_type,
            phase=phase,
            style=style,
            official_daily_loss_pct=Decimal(account["daily_loss_pct"]),
            official_max_loss_pct=Decimal(account["max_loss_pct"]),
            max_loss_mode=account["max_loss_mode"],
            internal_daily_stop_pct=Decimal(
                internal["daily_stop_pct"][account_type.value]
            ),
            internal_max_loss_stop_pct=Decimal(
                internal["max_loss_stop_pct"]
            ),
            warning_utilization_pct=Decimal(
                internal["warning_utilization_pct"]
            ),
            reduce_size_utilization_pct=Decimal(
                internal["reduce_size_utilization_pct"]
            ),
            lock_utilization_pct=Decimal(internal["lock_utilization_pct"]),
            single_trade_risk_pct=Decimal(
                internal["single_trade_risk_pct"]
            ),
            max_open_risk_pct=Decimal(internal["max_open_risk_pct"]),
            daily_buffer_fraction_per_trade=Decimal(
                internal["daily_buffer_fraction_per_trade"]
            ),
            max_loss_buffer_fraction_per_trade=Decimal(
                internal["max_loss_buffer_fraction_per_trade"]
            ),
            news_hard_before_minutes=int(
                news["ftmo_hard_before_minutes"]
            ),
            news_hard_after_minutes=int(news["ftmo_hard_after_minutes"]),
            news_internal_before_minutes=int(
                news["internal_before_minutes"]
            ),
            news_internal_after_minutes=int(news["internal_after_minutes"]),
            news_force_flat_before_minutes=int(
                news["force_flat_before_ftmo_window_minutes"]
            ),
            news_cancel_pending_before_minutes=int(
                news["cancel_pending_before_ftmo_window_minutes"]
            ),
            news_max_calendar_age_seconds=int(
                news["max_calendar_age_seconds"]
            ),
            max_opens_5m=int(frequency["max_opens_5m"]),
            max_opens_1h=int(frequency["max_opens_1h"]),
            max_opens_day=int(frequency["max_opens_day"]),
            warning_requests_day=int(frequency["warning_requests_day"]),
            stop_requests_day=int(frequency["stop_requests_day"]),
            min_modify_interval_seconds=int(
                frequency["min_modify_interval_seconds"]
            ),
            market_flat_before_minutes=int(
                market_close.get("force_flat_before_minutes", 10)
            ),
            market_gap_open_block_before_minutes=int(
                market_close.get("gap_open_block_before_minutes", 120)
            ),
            market_restricted_break_min_minutes=int(
                market_close.get("restricted_break_min_minutes", 120)
            ),
        )

    @classmethod
    def one_step_default(
        cls,
        phase: AccountPhase = AccountPhase.EVALUATION,
        style: AccountStyle = AccountStyle.STANDARD,
    ) -> "RuleProfile":
        if style == AccountStyle.SWING:
            raise ValueError("Swing style is only available for 2-Step accounts")
        return cls(
            account_type=AccountType.ONE_STEP,
            phase=phase,
            style=style,
            official_daily_loss_pct=Decimal("0.03"),
            official_max_loss_pct=Decimal("0.10"),
            max_loss_mode="eod_trailing",
            internal_daily_stop_pct=Decimal("0.024"),
            internal_max_loss_stop_pct=Decimal("0.080"),
        )

    @classmethod
    def two_step_default(
        cls,
        phase: AccountPhase = AccountPhase.EVALUATION,
        style: AccountStyle = AccountStyle.STANDARD,
    ) -> "RuleProfile":
        return cls(
            account_type=AccountType.TWO_STEP,
            phase=phase,
            style=style,
            official_daily_loss_pct=Decimal("0.05"),
            official_max_loss_pct=Decimal("0.10"),
            max_loss_mode="static",
            internal_daily_stop_pct=Decimal("0.040"),
            internal_max_loss_stop_pct=Decimal("0.080"),
        )


@dataclass(frozen=True)
class AccountSnapshot:
    initial_capital: Decimal
    day_start_balance: Decimal
    highest_settled_balance: Decimal
    balance: Decimal
    equity: Decimal
    as_of: datetime
    current_open_risk: Decimal = ZERO
    data_age_seconds: int = 0
    data_uncertain: bool = False
    day_locked: bool = False
    breach_latched: bool = False
    open_positions_count: int | None = None
    pending_orders_count: int | None = None


@dataclass(frozen=True)
class NewsEvent:
    event_id: str
    release_time: datetime
    affected_symbols: frozenset[str]
    importance: str = "high"
    source: str = "ftmo-calendar"

    def affects(self, symbol: str) -> bool:
        return any(
            _symbol_pattern_matches(item, symbol)
            for item in self.affected_symbols
        )


@dataclass(frozen=True)
class MarketClosure:
    closure_id: str
    start_time: datetime
    end_time: datetime
    affected_symbols: frozenset[str]
    source: str = "approved-market-schedule"

    def affects(self, symbol: str) -> bool:
        return any(
            _symbol_pattern_matches(item, symbol)
            for item in self.affected_symbols
        )


@dataclass
class FrequencyState:
    open_times: list[datetime] = field(default_factory=list)
    request_times: list[datetime] = field(default_factory=list)
    last_modify_by_symbol: dict[str, datetime] = field(default_factory=dict)

    def prune(self, now: datetime) -> None:
        retention_start = now - timedelta(days=2)
        self.open_times[:] = [
            item for item in self.open_times if item >= retention_start
        ]
        self.request_times[:] = [
            item for item in self.request_times if item >= retention_start
        ]
        self.last_modify_by_symbol = {
            symbol: item
            for symbol, item in self.last_modify_by_symbol.items()
            if item >= retention_start
        }

    def opens_in_last(self, now: datetime, window: timedelta) -> int:
        threshold = now - window
        return sum(item >= threshold for item in self.open_times)

    def opens_on_day(self, now: datetime, timezone: ZoneInfo) -> int:
        day = now.astimezone(timezone).date()
        return sum(item.astimezone(timezone).date() == day for item in self.open_times)

    def requests_on_day(self, now: datetime, timezone: ZoneInfo) -> int:
        day = now.astimezone(timezone).date()
        return sum(
            item.astimezone(timezone).date() == day
            for item in self.request_times
        )


@dataclass(frozen=True)
class TradeRequest:
    symbol: str
    action: Action
    requested_at: datetime
    volume: Decimal = ZERO
    entry_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    loss_per_volume_unit: Optional[Decimal] = None
    estimated_costs: Decimal = ZERO
    additional_risk: Decimal = ZERO
    is_risk_increasing: bool = True
    idea_id: str = ""

    @property
    def is_open(self) -> bool:
        return self.action == Action.OPEN


@dataclass(frozen=True)
class RiskBudget:
    total_budget: Decimal
    single_trade_cap: Decimal
    daily_buffer_cap: Decimal
    max_loss_buffer_cap: Decimal
    open_risk_buffer_cap: Decimal
    daily_utilization: Decimal
    max_loss_utilization: Decimal


@dataclass(frozen=True)
class PositionSize:
    volume: Decimal
    expected_loss: Decimal
    risk_budget: Decimal


@dataclass(frozen=True)
class Decision:
    code: DecisionCode
    reasons: tuple[str, ...] = ()
    risk_budget: Optional[RiskBudget] = None

    @property
    def allowed(self) -> bool:
        return self.code == DecisionCode.ALLOW


class RiskEngine:
    def __init__(self, profile: RuleProfile, day_timezone: str = "Europe/Prague"):
        self.profile = profile
        self.day_timezone = ZoneInfo(day_timezone)

    def daily_loss_limit(self, snapshot: AccountSnapshot) -> Decimal:
        return snapshot.day_start_balance - (
            snapshot.initial_capital * self.profile.official_daily_loss_pct
        )

    def max_loss_limit(self, snapshot: AccountSnapshot) -> Decimal:
        if self.profile.max_loss_mode == "static":
            reference = snapshot.initial_capital
        elif self.profile.max_loss_mode == "eod_trailing":
            reference = snapshot.highest_settled_balance
        else:
            raise ValueError(f"Unsupported max_loss_mode: {self.profile.max_loss_mode}")
        return reference - (
            snapshot.initial_capital * self.profile.official_max_loss_pct
        )

    def internal_daily_stop_limit(self, snapshot: AccountSnapshot) -> Decimal:
        return snapshot.day_start_balance - (
            snapshot.initial_capital * self.profile.internal_daily_stop_pct
        )

    def internal_max_loss_stop_limit(self, snapshot: AccountSnapshot) -> Decimal:
        if self.profile.max_loss_mode == "static":
            reference = snapshot.initial_capital
        else:
            reference = snapshot.highest_settled_balance
        return reference - (
            snapshot.initial_capital * self.profile.internal_max_loss_stop_pct
        )

    def daily_loss(self, snapshot: AccountSnapshot) -> Decimal:
        return max(ZERO, snapshot.day_start_balance - snapshot.equity)

    def max_loss(self, snapshot: AccountSnapshot) -> Decimal:
        return max(
            ZERO,
            self.max_loss_reference(snapshot) - snapshot.equity,
        )

    def daily_utilization(self, snapshot: AccountSnapshot) -> Decimal:
        allowance = snapshot.initial_capital * self.profile.official_daily_loss_pct
        return self.daily_loss(snapshot) / allowance if allowance else Decimal("1")

    def max_loss_utilization(self, snapshot: AccountSnapshot) -> Decimal:
        allowance = snapshot.initial_capital * self.profile.official_max_loss_pct
        loss_from_reference = max(ZERO, self.max_loss_reference(snapshot) - snapshot.equity)
        return (
            loss_from_reference / allowance if allowance else Decimal("1")
        )

    def max_loss_reference(self, snapshot: AccountSnapshot) -> Decimal:
        if self.profile.max_loss_mode == "static":
            return snapshot.initial_capital
        return snapshot.highest_settled_balance

    def status(self, snapshot: AccountSnapshot) -> str:
        if snapshot.breach_latched:
            return "BREACH"
        if snapshot.equity <= self.daily_loss_limit(snapshot):
            return "BREACH"
        if snapshot.equity <= self.max_loss_limit(snapshot):
            return "BREACH"
        if snapshot.day_locked:
            return "LOCKED"
        utilization = max(
            self.daily_utilization(snapshot),
            self.max_loss_utilization(snapshot),
        )
        if utilization >= self.profile.lock_utilization_pct:
            return "LOCKED"
        if utilization >= self.profile.reduce_size_utilization_pct:
            return "RED"
        if utilization >= self.profile.warning_utilization_pct:
            return "AMBER"
        return "GREEN"

    def risk_budget(self, snapshot: AccountSnapshot) -> RiskBudget:
        daily_internal_allowance = (
            snapshot.initial_capital * self.profile.internal_daily_stop_pct
        )
        daily_consumed = self.daily_loss(snapshot)
        daily_buffer = max(
            ZERO,
            daily_internal_allowance
            - daily_consumed
            - snapshot.current_open_risk,
        )
        max_buffer = max(
            ZERO,
            snapshot.equity
            - self.internal_max_loss_stop_limit(snapshot)
            - snapshot.current_open_risk,
        )
        open_buffer = max(
            ZERO,
            snapshot.initial_capital * self.profile.max_open_risk_pct
            - snapshot.current_open_risk,
        )
        base_budget = max(
            ZERO,
            min(
                snapshot.initial_capital * self.profile.single_trade_risk_pct,
                daily_buffer
                * self.profile.daily_buffer_fraction_per_trade,
                max_buffer
                * self.profile.max_loss_buffer_fraction_per_trade,
                open_buffer,
            ),
        )
        account_status = self.status(snapshot)
        if account_status in {"RED", "LOCKED", "BREACH"}:
            base_budget = ZERO
        elif account_status == "AMBER":
            base_budget *= Decimal("0.50")
        return RiskBudget(
            total_budget=base_budget,
            single_trade_cap=snapshot.initial_capital
            * self.profile.single_trade_risk_pct,
            daily_buffer_cap=daily_buffer
            * self.profile.daily_buffer_fraction_per_trade,
            max_loss_buffer_cap=max_buffer
            * self.profile.max_loss_buffer_fraction_per_trade,
            open_risk_buffer_cap=open_buffer,
            daily_utilization=self.daily_utilization(snapshot),
            max_loss_utilization=self.max_loss_utilization(snapshot),
        )

    def size_position(
        self,
        snapshot: AccountSnapshot,
        loss_per_volume_unit: Decimal,
        volume_step: Decimal,
        min_volume: Decimal,
        max_volume: Optional[Decimal] = None,
        estimated_costs: Decimal = ZERO,
    ) -> PositionSize:
        if loss_per_volume_unit <= ZERO:
            raise ValueError("loss_per_volume_unit must be positive")
        if volume_step <= ZERO:
            raise ValueError("volume_step must be positive")
        if min_volume <= ZERO:
            raise ValueError("min_volume must be positive")
        if estimated_costs < ZERO:
            raise ValueError("estimated_costs cannot be negative")
        if max_volume is not None and max_volume <= ZERO:
            raise ValueError("max_volume must be positive")
        if max_volume is not None and max_volume < min_volume:
            raise ValueError("max_volume cannot be below min_volume")
        budget = self.risk_budget(snapshot).total_budget
        volume_budget = max(ZERO, budget - estimated_costs)
        raw_volume = volume_budget / loss_per_volume_unit
        steps = (raw_volume / volume_step).to_integral_value(
            rounding=ROUND_DOWN
        )
        volume = steps * volume_step
        if max_volume is not None:
            volume = min(volume, max_volume)
        expected_loss = volume * loss_per_volume_unit + (
            estimated_costs if volume > ZERO else ZERO
        )
        if volume < min_volume:
            volume = ZERO
            expected_loss = ZERO
        return PositionSize(
            volume=volume,
            expected_loss=expected_loss,
            risk_budget=budget,
        )

    def evaluate(
        self,
        snapshot: AccountSnapshot,
        request: TradeRequest,
        frequency: FrequencyState,
        news_events: Iterable[NewsEvent] = (),
        market_closures: Iterable[MarketClosure] = (),
    ) -> Decision:
        if not request.symbol.strip():
            return Decision(
                DecisionCode.REJECT_RISK,
                ("symbol must not be empty",),
            )
        if request.requested_at.tzinfo is None:
            return Decision(
                DecisionCode.REJECT_DATA_STALE,
                ("request timestamp must include a timezone",),
            )
        if request.action == Action.OPEN and not request.is_risk_increasing:
            return Decision(
                DecisionCode.REJECT_RISK,
                ("open requests must be risk-increasing",),
            )
        if request.action in {Action.CLOSE, Action.CANCEL} and request.is_risk_increasing:
            return Decision(
                DecisionCode.REJECT_RISK,
                ("close and cancel requests must be risk-reducing",),
            )
        if request.volume < ZERO:
            return Decision(
                DecisionCode.REJECT_RISK,
                ("volume cannot be negative",),
            )
        if request.entry_price is not None and request.entry_price <= ZERO:
            return Decision(
                DecisionCode.REJECT_RISK,
                ("entry price must be positive when supplied",),
            )
        if request.stop_loss is not None and request.stop_loss <= ZERO:
            return Decision(
                DecisionCode.REJECT_STOP_LOSS,
                ("stop loss must be positive when supplied",),
            )
        if request.estimated_costs < ZERO or request.additional_risk < ZERO:
            return Decision(
                DecisionCode.REJECT_RISK,
                ("risk inputs cannot be negative",),
            )
        if not request.is_risk_increasing and request.additional_risk > ZERO:
            return Decision(
                DecisionCode.REJECT_RISK,
                ("risk-reducing requests cannot declare additional risk",),
            )
        if (
            request.is_risk_increasing
            and (snapshot.data_age_seconds > 5 or snapshot.data_uncertain)
        ):
            reason = (
                "account snapshot is older than 5 seconds"
                if snapshot.data_age_seconds > 5
                else "account settlement baseline is unconfirmed"
            )
            return Decision(
                DecisionCode.REJECT_DATA_STALE,
                (reason,),
            )

        official_reasons = []
        if snapshot.breach_latched:
            official_reasons.append(
                "an official loss breach was previously observed"
            )
        if snapshot.equity <= self.daily_loss_limit(snapshot):
            official_reasons.append("equity is below the official daily loss limit")
        if snapshot.equity <= self.max_loss_limit(snapshot):
            official_reasons.append("equity is below the official maximum loss limit")
        if official_reasons and request.is_risk_increasing:
            return Decision(
                DecisionCode.REJECT_OFFICIAL_BREACH,
                tuple(official_reasons),
            )

        status = self.status(snapshot)
        if status in {"RED", "LOCKED", "BREACH"} and request.is_risk_increasing:
            return Decision(
                DecisionCode.REJECT_INTERNAL_LOCK,
                (f"account status is {status}",),
            )

        news_decision = self._check_news(request, news_events)
        if news_decision is not None:
            return news_decision

        market_decision = self._check_market_close(request, market_closures)
        if market_decision is not None:
            return market_decision

        if request.is_open:
            if request.stop_loss is None:
                return Decision(
                    DecisionCode.REJECT_STOP_LOSS,
                    ("opening requests require a stop loss",),
                )
            if request.volume <= ZERO:
                return Decision(
                    DecisionCode.REJECT_RISK,
                    ("opening volume must be positive",),
                )
            if (
                request.entry_price is not None
                and request.stop_loss == request.entry_price
            ):
                return Decision(
                    DecisionCode.REJECT_STOP_LOSS,
                    ("stop loss must differ from entry price",),
                )

        frequency.prune(request.requested_at)
        frequency_reason = self._check_frequency(request, frequency)
        if frequency_reason is not None:
            return frequency_reason
        projected_requests_today = (
            frequency.requests_on_day(
                request.requested_at,
                self.day_timezone,
            )
            + 1
        )
        warnings = (
            ("daily server-request warning threshold reached",)
            if projected_requests_today
            >= self.profile.warning_requests_day
            else ()
        )

        budget = self.risk_budget(snapshot)
        if request.is_open:
            if request.loss_per_volume_unit is None:
                return Decision(
                    DecisionCode.REJECT_RISK,
                    ("opening requests require normalized loss per volume unit",),
                    budget,
                )
            if request.loss_per_volume_unit <= ZERO:
                return Decision(
                    DecisionCode.REJECT_RISK,
                    ("loss per volume unit must be positive",),
                    budget,
                )
            if request.estimated_costs < ZERO:
                return Decision(
                    DecisionCode.REJECT_RISK,
                    ("estimated costs cannot be negative",),
                    budget,
                )
            expected_loss = (
                request.volume * request.loss_per_volume_unit
                + request.estimated_costs
            )
            if expected_loss > budget.total_budget:
                return Decision(
                    DecisionCode.REJECT_RISK,
                    (
                        f"expected loss {expected_loss} exceeds budget "
                        f"{budget.total_budget}",
                    ),
                    budget,
                )
        elif request.action == Action.MODIFY and request.is_risk_increasing:
            if request.additional_risk <= ZERO:
                return Decision(
                    DecisionCode.REJECT_RISK,
                    (
                        "risk-increasing modifications require a positive "
                        "additional_risk",
                    ),
                    budget,
                )
            if request.additional_risk > budget.total_budget:
                return Decision(
                    DecisionCode.REJECT_RISK,
                    (
                        f"additional risk {request.additional_risk} exceeds "
                        f"budget {budget.total_budget}",
                    ),
                    budget,
                )

        return Decision(
            DecisionCode.ALLOW,
            reasons=warnings,
            risk_budget=budget,
        )

    def _check_frequency(
        self,
        request: TradeRequest,
        frequency: FrequencyState,
    ) -> Optional[Decision]:
        requests_today = frequency.requests_on_day(
            request.requested_at, self.day_timezone
        )
        if requests_today >= self.profile.stop_requests_day:
            return Decision(
                DecisionCode.REJECT_FREQUENCY,
                ("daily server-request stop threshold reached",),
            )

        if not request.is_open:
            if request.action == Action.MODIFY:
                last_modify = frequency.last_modify_by_symbol.get(
                    request.symbol.upper()
                )
                if (
                    last_modify is not None
                    and request.requested_at - last_modify
                    < timedelta(seconds=self.profile.min_modify_interval_seconds)
                ):
                    return Decision(
                        DecisionCode.REJECT_FREQUENCY,
                        ("symbol modification cooldown is active",),
                    )
            return None

        opens_5m = frequency.opens_in_last(
            request.requested_at, timedelta(minutes=5)
        )
        opens_1h = frequency.opens_in_last(
            request.requested_at, timedelta(hours=1)
        )
        opens_day = frequency.opens_on_day(
            request.requested_at, self.day_timezone
        )
        reasons = []
        if opens_5m >= self.profile.max_opens_5m:
            reasons.append("5-minute opening limit reached")
        if opens_1h >= self.profile.max_opens_1h:
            reasons.append("1-hour opening limit reached")
        if opens_day >= self.profile.max_opens_day:
            reasons.append("daily opening limit reached")
        if reasons:
            return Decision(DecisionCode.REJECT_FREQUENCY, tuple(reasons))
        return None

    def _check_news(
        self,
        request: TradeRequest,
        news_events: Iterable[NewsEvent],
    ) -> Optional[Decision]:
        if request.action == Action.CANCEL:
            return None

        for event in news_events:
            if not event.affects(request.symbol):
                continue
            delta = request.requested_at - event.release_time
            hard_start = -timedelta(minutes=self.profile.news_hard_before_minutes)
            hard_end = timedelta(minutes=self.profile.news_hard_after_minutes)
            internal_start = -timedelta(
                minutes=self.profile.news_internal_before_minutes
            )
            internal_end = timedelta(
                minutes=self.profile.news_internal_after_minutes
            )

            if (
                self.profile.phase == AccountPhase.FTMO_ACCOUNT
                and self.profile.style == AccountStyle.STANDARD
                and hard_start <= delta <= hard_end
            ):
                return Decision(
                    DecisionCode.REJECT_NEWS,
                    (
                        f"{event.event_id} is inside the FTMO hard news window",
                    ),
                )

            if internal_start <= delta <= internal_end and (
                request.action == Action.OPEN or request.is_risk_increasing
            ):
                return Decision(
                    DecisionCode.REJECT_NEWS,
                    (
                        f"{event.event_id} is inside the internal news buffer",
                    ),
                )
        return None

    def _check_market_close(
        self,
        request: TradeRequest,
        market_closures: Iterable[MarketClosure],
    ) -> Optional[Decision]:
        if request.action == Action.CANCEL:
            return None
        open_block_before = timedelta(
            minutes=self.profile.market_gap_open_block_before_minutes
        )
        for closure in market_closures:
            if not closure.affects(request.symbol):
                continue
            if (
                closure.end_time - closure.start_time
                < timedelta(
                    minutes=self.profile.market_restricted_break_min_minutes
                )
            ):
                continue
            until_start = closure.start_time - request.requested_at
            inside_closure = (
                closure.start_time
                <= request.requested_at
                <= closure.end_time
            )
            if inside_closure:
                if request.is_risk_increasing:
                    return Decision(
                        DecisionCode.REJECT_MARKET_CLOSE,
                        (
                            f"{closure.closure_id} market closure is active",
                        ),
                    )
            if (
                timedelta(0) <= until_start <= open_block_before
                and request.is_risk_increasing
            ):
                return Decision(
                    DecisionCode.REJECT_MARKET_CLOSE,
                    (
                        f"{closure.closure_id} market closure is approaching",
                    ),
                )
        return None


def ftmo_day_key(
    timestamp: datetime,
    timezone_name: str = "Europe/Prague",
) -> str:
    """Return the FTMO calendar day using the configured timezone."""
    local = timestamp.astimezone(ZoneInfo(timezone_name))
    return local.date().isoformat()


def validate_config(config: Mapping[str, Any]) -> None:
    """Validate all safety-critical configuration before serving requests."""

    rule_version = config.get("rule_version")
    if not isinstance(rule_version, str) or not rule_version.strip():
        raise ValueError("rule_version must be a non-empty string")

    timezone_name = config.get("ftmo_day_timezone", "Europe/Prague")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("ftmo_day_timezone must be a non-empty string")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(
            f"ftmo_day_timezone is not a valid IANA timezone: {timezone_name}"
        ) from exc

    accounts = config.get("accounts")
    if not isinstance(accounts, Mapping):
        raise ValueError("accounts must be an object")
    for account_type in (AccountType.ONE_STEP.value, AccountType.TWO_STEP.value):
        account = accounts.get(account_type)
        if not isinstance(account, Mapping):
            raise ValueError(f"accounts.{account_type} must be an object")
        daily_loss = _config_decimal(
            account.get("daily_loss_pct"),
            f"accounts.{account_type}.daily_loss_pct",
        )
        max_loss = _config_decimal(
            account.get("max_loss_pct"),
            f"accounts.{account_type}.max_loss_pct",
        )
        if not ZERO < daily_loss < Decimal("1"):
            raise ValueError(
                f"accounts.{account_type}.daily_loss_pct must be between 0 and 1"
            )
        if not ZERO < max_loss < Decimal("1"):
            raise ValueError(
                f"accounts.{account_type}.max_loss_pct must be between 0 and 1"
            )
        mode = account.get("max_loss_mode")
        expected_mode = (
            "eod_trailing"
            if account_type == AccountType.ONE_STEP.value
            else "static"
        )
        if mode != expected_mode:
            raise ValueError(
                f"accounts.{account_type}.max_loss_mode must be {expected_mode}"
            )

    internal = config.get("internal_controls")
    if not isinstance(internal, Mapping):
        raise ValueError("internal_controls must be an object")
    daily_stop = internal.get("daily_stop_pct")
    if not isinstance(daily_stop, Mapping):
        raise ValueError("internal_controls.daily_stop_pct must be an object")
    for account_type in (AccountType.ONE_STEP.value, AccountType.TWO_STEP.value):
        daily_stop_pct = _config_decimal(
            daily_stop.get(account_type),
            f"internal_controls.daily_stop_pct.{account_type}",
        )
        official_daily = _config_decimal(
            accounts[account_type].get("daily_loss_pct"),
            f"accounts.{account_type}.daily_loss_pct",
        )
        if not ZERO < daily_stop_pct < official_daily:
            raise ValueError(
                "internal daily stop must be positive and below the official "
                f"daily loss for {account_type}"
            )

    internal_max = _config_decimal(
        internal.get("max_loss_stop_pct"),
        "internal_controls.max_loss_stop_pct",
    )
    official_max_values = [
        _config_decimal(
            accounts[item].get("max_loss_pct"),
            f"accounts.{item}.max_loss_pct",
        )
        for item in (AccountType.ONE_STEP.value, AccountType.TWO_STEP.value)
    ]
    if not ZERO < internal_max < min(official_max_values):
        raise ValueError(
            "internal_controls.max_loss_stop_pct must be below every official "
            "maximum loss percentage"
        )

    warning = _config_decimal(
        internal.get("warning_utilization_pct"),
        "internal_controls.warning_utilization_pct",
    )
    reduce_size = _config_decimal(
        internal.get("reduce_size_utilization_pct"),
        "internal_controls.reduce_size_utilization_pct",
    )
    lock = _config_decimal(
        internal.get("lock_utilization_pct"),
        "internal_controls.lock_utilization_pct",
    )
    if not ZERO < warning < reduce_size < lock < Decimal("1"):
        raise ValueError(
            "utilization thresholds must satisfy 0 < warning < reduce < lock < 1"
        )

    for setting_name in (
        "single_trade_risk_pct",
        "max_open_risk_pct",
        "daily_buffer_fraction_per_trade",
        "max_loss_buffer_fraction_per_trade",
    ):
        value = _config_decimal(
            internal.get(setting_name),
            f"internal_controls.{setting_name}",
        )
        if not ZERO < value <= Decimal("1"):
            raise ValueError(
                f"internal_controls.{setting_name} must be greater than 0 "
                "and at most 1"
            )

    news = config.get("news_controls")
    if not isinstance(news, Mapping):
        raise ValueError("news_controls must be an object")
    _validate_nonnegative_int(
        news.get("ftmo_hard_before_minutes"),
        "news_controls.ftmo_hard_before_minutes",
    )
    _validate_nonnegative_int(
        news.get("ftmo_hard_after_minutes"),
        "news_controls.ftmo_hard_after_minutes",
    )
    _validate_nonnegative_int(
        news.get("internal_before_minutes"),
        "news_controls.internal_before_minutes",
    )
    _validate_nonnegative_int(
        news.get("internal_after_minutes"),
        "news_controls.internal_after_minutes",
    )
    _validate_nonnegative_int(
        news.get("force_flat_before_ftmo_window_minutes"),
        "news_controls.force_flat_before_ftmo_window_minutes",
    )
    _validate_nonnegative_int(
        news.get("cancel_pending_before_ftmo_window_minutes"),
        "news_controls.cancel_pending_before_ftmo_window_minutes",
    )
    _validate_positive_int(
        news.get("max_calendar_age_seconds"),
        "news_controls.max_calendar_age_seconds",
    )
    hard_before = int(news["ftmo_hard_before_minutes"])
    hard_after = int(news["ftmo_hard_after_minutes"])
    internal_before = int(news["internal_before_minutes"])
    internal_after = int(news["internal_after_minutes"])
    force_flat_before = int(
        news["force_flat_before_ftmo_window_minutes"]
    )
    cancel_pending_before = int(
        news["cancel_pending_before_ftmo_window_minutes"]
    )
    if hard_before > internal_before or hard_after > internal_after:
        raise ValueError(
            "FTMO hard news windows cannot exceed the internal news windows"
        )
    if not hard_before <= force_flat_before <= internal_before:
        raise ValueError(
            "force_flat_before_ftmo_window_minutes must be between the "
            "hard-before and internal-before windows"
        )
    if not hard_before <= cancel_pending_before <= internal_before:
        raise ValueError(
            "cancel_pending_before_ftmo_window_minutes must be between the "
            "hard-before and internal-before windows"
        )
    if news.get("restricted_account_phase") != AccountPhase.FTMO_ACCOUNT.value:
        raise ValueError(
            "news_controls.restricted_account_phase must be ftmo_account"
        )
    if news.get("restricted_account_style") != AccountStyle.STANDARD.value:
        raise ValueError(
            "news_controls.restricted_account_style must be standard"
        )

    frequency = config.get("frequency_controls")
    if not isinstance(frequency, Mapping):
        raise ValueError("frequency_controls must be an object")
    for setting_name in (
        "max_opens_5m",
        "max_opens_1h",
        "max_opens_day",
        "warning_requests_day",
        "stop_requests_day",
    ):
        _validate_positive_int(
            frequency.get(setting_name),
            f"frequency_controls.{setting_name}",
        )
    if not (
        int(frequency["max_opens_5m"])
        <= int(frequency["max_opens_1h"])
        <= int(frequency["max_opens_day"])
    ):
        raise ValueError(
            "opening limits must satisfy max_opens_5m <= max_opens_1h "
            "<= max_opens_day"
        )
    if int(frequency["warning_requests_day"]) >= int(
        frequency["stop_requests_day"]
    ):
        raise ValueError(
            "warning_requests_day must be below stop_requests_day"
        )
    _validate_nonnegative_int(
        frequency.get("min_modify_interval_seconds"),
        "frequency_controls.min_modify_interval_seconds",
    )

    market_close = config.get("market_close_controls", {})
    if not isinstance(market_close, Mapping):
        raise ValueError("market_close_controls must be an object")
    for setting_name in (
        "force_flat_before_minutes",
        "gap_open_block_before_minutes",
        "restricted_break_min_minutes",
        "max_schedule_age_seconds",
    ):
        _validate_nonnegative_int(
            market_close.get(setting_name),
            f"market_close_controls.{setting_name}",
        )
    if int(market_close["force_flat_before_minutes"]) > int(
        market_close["gap_open_block_before_minutes"]
    ):
        raise ValueError(
            "force_flat_before_minutes cannot exceed "
            "gap_open_block_before_minutes"
        )

    qualification = config.get("qualification_controls")
    if not isinstance(qualification, Mapping):
        raise ValueError("qualification_controls must be an object")
    _validate_positive_int(
        qualification.get("max_account_snapshot_age_seconds"),
        "qualification_controls.max_account_snapshot_age_seconds",
    )
    one_step = qualification.get(AccountType.ONE_STEP.value)
    two_step = qualification.get(AccountType.TWO_STEP.value)
    if not isinstance(one_step, Mapping):
        raise ValueError("qualification_controls.one_step must be an object")
    if not isinstance(two_step, Mapping):
        raise ValueError("qualification_controls.two_step must be an object")
    phase_maps = (
        (
            AccountType.ONE_STEP.value,
            one_step,
            (AccountPhase.EVALUATION.value, AccountPhase.FTMO_ACCOUNT.value),
        ),
        (
            AccountType.TWO_STEP.value,
            two_step,
            (
                AccountPhase.EVALUATION.value,
                AccountPhase.VERIFICATION.value,
                AccountPhase.FTMO_ACCOUNT.value,
            ),
        ),
    )
    for qualification_type, account_controls, phases in phase_maps:
        for phase_name in phases:
            controls = account_controls.get(phase_name)
            prefix = (
                f"qualification_controls.{qualification_type}.{phase_name}"
            )
            if not isinstance(controls, Mapping):
                raise ValueError(f"{prefix} must be an object")
            target_value = controls.get("profit_target_pct")
            if target_value is not None:
                target = _config_decimal(
                    target_value,
                    f"{prefix}.profit_target_pct",
                )
                if not ZERO < target < Decimal("1"):
                    raise ValueError(
                        f"{prefix}.profit_target_pct must be between 0 and 1"
                    )
            _validate_nonnegative_int(
                controls.get("minimum_trading_days"),
                f"{prefix}.minimum_trading_days",
            )
            best_day = controls.get("best_day_rule_pct")
            if best_day is not None:
                best_day_decimal = _config_decimal(
                    best_day,
                    f"{prefix}.best_day_rule_pct",
                )
                if not ZERO < best_day_decimal <= Decimal("1"):
                    raise ValueError(
                        f"{prefix}.best_day_rule_pct must be between 0 and 1"
                    )
    security = config.get("security", {})
    if not isinstance(security, Mapping):
        raise ValueError("security must be an object")
    if not isinstance(
        security.get("require_account_credentials", True),
        bool,
    ):
        raise ValueError("security.require_account_credentials must be a boolean")
    if not isinstance(security.get("require_mtls", False), bool):
        raise ValueError("security.require_mtls must be a boolean")
    for security_field in (
        "credential_default_ttl_seconds",
        "credential_max_ttl_seconds",
        "credential_rotation_overlap_seconds",
    ):
        _validate_positive_int(
            security.get(security_field),
            f"security.{security_field}",
        )
    if int(security["credential_default_ttl_seconds"]) > int(
        security["credential_max_ttl_seconds"]
    ):
        raise ValueError(
            "security.credential_default_ttl_seconds cannot exceed "
            "credential_max_ttl_seconds"
        )


def _config_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed


def _validate_positive_int(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")


def _validate_nonnegative_int(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def load_profile(
    path: str | Path,
    account_type: AccountType,
    phase: AccountPhase,
    style: AccountStyle,
) -> RuleProfile:
    with Path(path).open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    return RuleProfile.from_config(config, account_type, phase, style)
