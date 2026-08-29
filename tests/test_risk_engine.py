import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.risk_engine import (
    AccountType,
    AccountPhase,
    AccountSnapshot,
    AccountStyle,
    Action,
    DecisionCode,
    FrequencyState,
    MarketClosure,
    NewsEvent,
    RiskEngine,
    RuleProfile,
    TradeRequest,
    TradeSide,
    ftmo_day_key,
    load_profile,
    validate_config,
)


UTC = timezone.utc


def snapshot(
    *,
    equity: str = "100000",
    day_start_balance: str = "100000",
    highest_settled_balance: str = "100000",
    open_risk: str = "0",
    age: int = 0,
    uncertain: bool = False,
    day_locked: bool = False,
    breach_latched: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        initial_capital=Decimal("100000"),
        day_start_balance=Decimal(day_start_balance),
        highest_settled_balance=Decimal(highest_settled_balance),
        balance=Decimal("100000"),
        equity=Decimal(equity),
        as_of=datetime(2026, 8, 22, 12, 0, tzinfo=UTC),
        current_open_risk=Decimal(open_risk),
        data_age_seconds=age,
        data_uncertain=uncertain,
        day_locked=day_locked,
        breach_latched=breach_latched,
    )


def open_request(
    *,
    when: datetime,
    volume: str = "1",
    symbol: str = "EURUSD",
    loss_per_unit: str = "100",
    stop_loss: str = "1.0800",
) -> TradeRequest:
    return TradeRequest(
        symbol=symbol,
        action=Action.OPEN,
        requested_at=when,
        side=TradeSide.BUY,
        volume=Decimal(volume),
        entry_price=Decimal("1.1000"),
        stop_loss=Decimal(stop_loss),
        loss_per_volume_unit=Decimal(loss_per_unit),
    )


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
        self.frequency = FrequencyState()

    def test_one_step_official_limits(self):
        engine = RiskEngine(RuleProfile.one_step_default())
        self.assertEqual(engine.daily_loss_limit(snapshot()), Decimal("97000"))
        self.assertEqual(engine.max_loss_limit(snapshot()), Decimal("90000"))
        self.assertEqual(engine.status(snapshot(equity="98500")), "AMBER")

    def test_two_step_daily_limit_is_five_percent(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        self.assertEqual(engine.daily_loss_limit(snapshot()), Decimal("95000"))
        self.assertEqual(engine.max_loss_limit(snapshot()), Decimal("90000"))

    def test_one_step_trailing_reference_uses_settled_balance(self):
        engine = RiskEngine(RuleProfile.one_step_default())
        self.assertEqual(
            engine.max_loss_limit(snapshot(highest_settled_balance="105000")),
            Decimal("95000"),
        )

    def test_official_breach_rejects_new_risk(self):
        engine = RiskEngine(RuleProfile.one_step_default())
        decision = engine.evaluate(
            snapshot(equity="96999"),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_OFFICIAL_BREACH)

    def test_touching_official_limit_is_a_breach(self):
        engine = RiskEngine(RuleProfile.one_step_default())
        decision = engine.evaluate(
            snapshot(equity="97000"),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_OFFICIAL_BREACH)

    def test_official_breach_still_allows_risk_reducing_close(self):
        engine = RiskEngine(RuleProfile.one_step_default())
        request = TradeRequest(
            symbol="EURUSD",
            action=Action.CLOSE,
            requested_at=self.now,
            is_risk_increasing=False,
        )
        decision = engine.evaluate(
            snapshot(equity="96900"),
            request,
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.ALLOW)

    def test_latched_official_breach_rejects_after_equity_recovers(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        decision = engine.evaluate(
            snapshot(breach_latched=True),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_OFFICIAL_BREACH)

    def test_daily_lock_rejects_after_equity_recovers(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        decision = engine.evaluate(
            snapshot(day_locked=True),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_INTERNAL_LOCK)

    def test_no_stop_loss_is_rejected(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        request = open_request(when=self.now)
        request = TradeRequest(**{**request.__dict__, "stop_loss": None})
        decision = engine.evaluate(snapshot(), request, self.frequency)
        self.assertEqual(decision.code, DecisionCode.REJECT_STOP_LOSS)

    def test_non_positive_loss_per_unit_is_rejected(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        decision = engine.evaluate(
            snapshot(),
            open_request(when=self.now, loss_per_unit="0"),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_RISK)

    def test_negative_volume_is_rejected_before_frequency_or_risk_budget(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        request = TradeRequest(
            **{
                **open_request(when=self.now).__dict__,
                "volume": Decimal("-1"),
            }
        )
        decision = engine.evaluate(snapshot(), request, self.frequency)
        self.assertEqual(decision.code, DecisionCode.REJECT_RISK)

    def test_position_size_rounds_down_to_volume_step(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        sized = engine.size_position(
            snapshot(),
            loss_per_volume_unit=Decimal("100"),
            volume_step=Decimal("0.01"),
            min_volume=Decimal("0.01"),
        )
        self.assertEqual(sized.volume, Decimal("2.50"))
        self.assertLessEqual(sized.expected_loss, sized.risk_budget)

    def test_position_size_reserves_estimated_costs(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        sized = engine.size_position(
            snapshot(),
            loss_per_volume_unit=Decimal("100"),
            volume_step=Decimal("0.01"),
            min_volume=Decimal("0.01"),
            estimated_costs=Decimal("50"),
        )
        self.assertEqual(sized.volume, Decimal("2.00"))
        self.assertEqual(sized.expected_loss, Decimal("250"))

    def test_risk_budget_shrinks_when_daily_loss_increases(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        budget = engine.risk_budget(snapshot(equity="98000"))
        self.assertEqual(budget.total_budget, Decimal("250"))

    def test_existing_open_risk_reduces_daily_and_max_buffers(self):
        engine = RiskEngine(RuleProfile.one_step_default())
        budget = engine.risk_budget(
            snapshot(
                equity="98500",
                open_risk="800",
            )
        )
        self.assertEqual(budget.daily_buffer_cap, Decimal("20.00"))
        self.assertEqual(budget.total_budget, Decimal("10.000"))

    def test_internal_lock_rejects_new_risk(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        decision = engine.evaluate(
            snapshot(equity="95999"),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_INTERNAL_LOCK)

    def test_red_status_rejects_new_risk(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        decision = engine.evaluate(
            snapshot(equity="96500"),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_INTERNAL_LOCK)

    def test_amber_status_halves_risk_budget(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        budget = engine.risk_budget(snapshot(equity="97500"))
        self.assertEqual(budget.total_budget, Decimal("125"))

    def test_ftmo_account_standard_rejects_hard_news_window(self):
        engine = RiskEngine(
            RuleProfile.two_step_default(
                phase=AccountPhase.FTMO_ACCOUNT,
                style=AccountStyle.STANDARD,
            )
        )
        event = NewsEvent(
            event_id="NFP",
            release_time=self.now,
            affected_symbols=frozenset({"EURUSD"}),
        )
        decision = engine.evaluate(
            snapshot(),
            open_request(when=self.now + timedelta(minutes=1)),
            self.frequency,
            [event],
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_NEWS)

    def test_ftmo_account_standard_blocks_close_and_modify_in_hard_window(self):
        engine = RiskEngine(
            RuleProfile.two_step_default(
                phase=AccountPhase.FTMO_ACCOUNT,
                style=AccountStyle.STANDARD,
            )
        )
        event = NewsEvent(
            event_id="CPI",
            release_time=self.now,
            affected_symbols=frozenset({"EURUSD"}),
        )
        for action in (Action.CLOSE, Action.MODIFY):
            with self.subTest(action=action):
                request = TradeRequest(
                    symbol="EURUSD",
                    action=action,
                    requested_at=self.now + timedelta(minutes=1),
                    stop_loss=Decimal("1.0800")
                    if action == Action.MODIFY
                    else None,
                    is_risk_increasing=False,
                )
                decision = engine.evaluate(
                    snapshot(),
                    request,
                    self.frequency,
                    [event],
                )
                self.assertEqual(decision.code, DecisionCode.REJECT_NEWS)

    def test_cancel_remains_allowed_in_hard_news_window(self):
        engine = RiskEngine(
            RuleProfile.two_step_default(
                phase=AccountPhase.FTMO_ACCOUNT,
                style=AccountStyle.STANDARD,
            )
        )
        event = NewsEvent(
            event_id="CPI",
            release_time=self.now,
            affected_symbols=frozenset({"EURUSD"}),
        )
        request = TradeRequest(
            symbol="EURUSD",
            action=Action.CANCEL,
            requested_at=self.now + timedelta(minutes=1),
            is_risk_increasing=False,
        )
        decision = engine.evaluate(
            snapshot(),
            request,
            self.frequency,
            [event],
        )
        self.assertEqual(decision.code, DecisionCode.ALLOW)

    def test_calendar_symbol_patterns_cover_broker_suffixes_and_all_symbols(self):
        event = NewsEvent(
            event_id="NFP",
            release_time=self.now,
            affected_symbols=frozenset({"EURUSD*"}),
        )
        closure = MarketClosure(
            closure_id="all-markets",
            start_time=self.now,
            end_time=self.now + timedelta(days=2),
            affected_symbols=frozenset({"*"}),
        )
        self.assertTrue(event.affects("EURUSD.a"))
        self.assertFalse(event.affects("GBPUSD"))
        self.assertTrue(closure.affects("US30.cash"))

    def test_evaluation_does_not_apply_ftmo_hard_news_rule_but_internal_buffer_applies(
        self,
    ):
        engine = RiskEngine(
            RuleProfile.two_step_default(phase=AccountPhase.EVALUATION)
        )
        event = NewsEvent(
            event_id="CPI",
            release_time=self.now,
            affected_symbols=frozenset({"EURUSD"}),
        )
        decision = engine.evaluate(
            snapshot(),
            open_request(when=self.now + timedelta(minutes=1)),
            self.frequency,
            [event],
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_NEWS)

    def test_swing_account_allows_close_inside_ftmo_hard_window(self):
        engine = RiskEngine(
            RuleProfile.two_step_default(
                phase=AccountPhase.FTMO_ACCOUNT,
                style=AccountStyle.SWING,
            )
        )
        event = NewsEvent(
            event_id="CPI",
            release_time=self.now,
            affected_symbols=frozenset({"EURUSD"}),
        )
        request = TradeRequest(
            symbol="EURUSD",
            action=Action.CLOSE,
            requested_at=self.now + timedelta(minutes=1),
            is_risk_increasing=False,
        )
        decision = engine.evaluate(
            snapshot(),
            request,
            self.frequency,
            [event],
        )
        self.assertEqual(decision.code, DecisionCode.ALLOW)

    def test_standard_ftmo_account_rejects_open_before_long_market_close(self):
        engine = RiskEngine(
            RuleProfile.two_step_default(
                phase=AccountPhase.FTMO_ACCOUNT,
                style=AccountStyle.STANDARD,
            )
        )
        closure = MarketClosure(
            closure_id="weekend-close",
            start_time=self.now + timedelta(minutes=5),
            end_time=self.now + timedelta(days=2),
            affected_symbols=frozenset({"EURUSD"}),
        )
        decision = engine.evaluate(
            snapshot(),
            open_request(when=self.now),
            self.frequency,
            market_closures=[closure],
        )
        self.assertEqual(
            decision.code,
            DecisionCode.REJECT_MARKET_CLOSE,
        )

    def test_swing_account_still_obeys_gap_open_guard(self):
        engine = RiskEngine(
            RuleProfile.two_step_default(
                phase=AccountPhase.FTMO_ACCOUNT,
                style=AccountStyle.SWING,
            )
        )
        closure = MarketClosure(
            closure_id="weekend-close",
            start_time=self.now + timedelta(minutes=5),
            end_time=self.now + timedelta(days=2),
            affected_symbols=frozenset({"EURUSD"}),
        )
        decision = engine.evaluate(
            snapshot(),
            open_request(when=self.now),
            self.frequency,
            market_closures=[closure],
        )
        self.assertEqual(
            decision.code,
            DecisionCode.REJECT_MARKET_CLOSE,
        )

    def test_one_step_swing_profile_is_invalid(self):
        with self.assertRaises(ValueError):
            RuleProfile.one_step_default(style=AccountStyle.SWING)

    def test_frequency_limit_rejects_fourth_open_in_five_minutes(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        self.frequency.open_times = [
            self.now - timedelta(minutes=1),
            self.now - timedelta(minutes=2),
            self.now - timedelta(minutes=3),
        ]
        decision = engine.evaluate(
            snapshot(),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_FREQUENCY)

    def test_request_warning_threshold_is_reported_on_allowed_request(self):
        profile = replace(
            RuleProfile.two_step_default(),
            warning_requests_day=2,
            stop_requests_day=3,
        )
        engine = RiskEngine(profile)
        self.frequency.request_times = [self.now - timedelta(minutes=1)]
        decision = engine.evaluate(
            snapshot(),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.ALLOW)
        self.assertIn(
            "daily server-request warning threshold reached",
            decision.reasons,
        )

    def test_daily_frequency_uses_prague_calendar_day(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        now = datetime(2026, 8, 22, 22, 30, tzinfo=UTC)
        previous_prague_day = datetime(2026, 8, 22, 21, 30, tzinfo=UTC)
        self.frequency.open_times = [
            previous_prague_day - timedelta(minutes=index)
            for index in range(30)
        ]
        decision = engine.evaluate(
            snapshot(),
            open_request(when=now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.ALLOW)

    def test_stale_account_data_is_fail_closed(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        decision = engine.evaluate(
            snapshot(age=6),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_DATA_STALE)

    def test_stale_account_data_still_allows_close(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        request = TradeRequest(
            symbol="EURUSD",
            action=Action.CLOSE,
            requested_at=self.now,
            is_risk_increasing=False,
        )
        decision = engine.evaluate(
            snapshot(age=6),
            request,
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.ALLOW)

    def test_uncertain_settlement_is_fail_closed_for_new_risk(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        decision = engine.evaluate(
            snapshot(uncertain=True),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_DATA_STALE)

    def test_negative_equity_is_evaluated_as_an_official_breach(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        decision = engine.evaluate(
            snapshot(equity="-1"),
            open_request(when=self.now),
            self.frequency,
        )
        self.assertEqual(decision.code, DecisionCode.REJECT_OFFICIAL_BREACH)

    def test_modify_cooldown_is_enforced(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        self.frequency.last_modify_by_symbol["EURUSD"] = (
            self.now - timedelta(seconds=5)
        )
        request = TradeRequest(
            symbol="EURUSD",
            action=Action.MODIFY,
            requested_at=self.now,
            is_risk_increasing=True,
        )
        decision = engine.evaluate(snapshot(), request, self.frequency)
        self.assertEqual(decision.code, DecisionCode.REJECT_FREQUENCY)

    def test_risk_increasing_modify_requires_additional_risk(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        request = TradeRequest(
            symbol="EURUSD",
            action=Action.MODIFY,
            requested_at=self.now,
            is_risk_increasing=True,
        )
        decision = engine.evaluate(snapshot(), request, FrequencyState())
        self.assertEqual(decision.code, DecisionCode.REJECT_RISK)

    def test_risk_increasing_modify_is_capped_by_budget(self):
        engine = RiskEngine(RuleProfile.two_step_default())
        request = TradeRequest(
            symbol="EURUSD",
            action=Action.MODIFY,
            requested_at=self.now,
            additional_risk=Decimal("251"),
            is_risk_increasing=True,
        )
        decision = engine.evaluate(snapshot(), request, FrequencyState())
        self.assertEqual(decision.code, DecisionCode.REJECT_RISK)

    def test_profile_can_be_loaded_from_json_config(self):
        profile = load_profile(
            "config/ftmo-v2.json",
            account_type=AccountType.ONE_STEP,
            phase=AccountPhase.FTMO_ACCOUNT,
            style=AccountStyle.STANDARD,
        )
        self.assertEqual(profile.official_daily_loss_pct, Decimal("0.03"))
        self.assertEqual(profile.internal_daily_stop_pct, Decimal("0.024"))

    def test_ftmo_day_key_uses_prague_timezone(self):
        timestamp = datetime(2026, 8, 22, 22, 30, tzinfo=UTC)
        self.assertEqual(ftmo_day_key(timestamp), "2026-08-23")

    def test_invalid_config_is_rejected_before_runtime(self):
        invalid = {
            "rule_version": "",
            "ftmo_day_timezone": "Europe/Prague",
            "accounts": {},
        }
        with self.assertRaises(ValueError):
            validate_config(invalid)


if __name__ == "__main__":
    unittest.main()
