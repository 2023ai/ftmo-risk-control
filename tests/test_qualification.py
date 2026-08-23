import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from src.qualification import qualification_snapshot
from src.risk_engine import (
    AccountPhase,
    AccountSnapshot,
    AccountStyle,
    AccountType,
)
from src.state_store import StoredAccount


UTC = timezone.utc


def _account(
    *,
    account_type: AccountType,
    phase: AccountPhase,
    uncertain: bool = False,
) -> StoredAccount:
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    return StoredAccount(
        account_id="qualification-account",
        account_type=account_type,
        phase=phase,
        style=AccountStyle.STANDARD,
        ftmo_day="2026-08-23",
        snapshot=AccountSnapshot(
            initial_capital=Decimal("100000"),
            day_start_balance=Decimal("100000"),
            highest_settled_balance=Decimal("100000"),
            balance=Decimal("100000"),
            equity=Decimal("100000"),
            as_of=now,
            data_uncertain=uncertain,
        ),
    )


def _trades(*profits: str) -> list[dict[str, str]]:
    return [
        {
            "trade_id": f"trade-{index}",
            "ftmo_day": f"2026-08-{20 + index:02d}",
            "net_profit": profit,
        }
        for index, profit in enumerate(profits)
    ]


def _history_status(phase: AccountPhase) -> dict[str, str]:
    return {
        "phase": phase.value,
        "cycle_id": "cycle-1",
        "history_start_at": "2026-08-01T00:00:00+00:00",
        "complete_through": "2026-08-23T12:00:00+00:00",
        "source": "test-history",
    }


def _trading_days(count: int) -> list[dict[str, str]]:
    return [
        {"ftmo_day": f"2026-08-{19 + index:02d}"}
        for index in range(count)
    ]


class QualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("config/ftmo-v2.json", encoding="utf-8") as handle:
            cls.config = json.load(handle)

    def test_one_step_best_day_and_profit_target_can_be_eligible(self):
        result = qualification_snapshot(
            config=self.config,
            account=_account(
                account_type=AccountType.ONE_STEP,
                phase=AccountPhase.EVALUATION,
            ),
            closed_trades=_trades("6000", "3000", "3000"),
            trading_day_events=[],
            history_status=_history_status(AccountPhase.EVALUATION),
        )
        self.assertTrue(result["profit_target"]["met"])
        self.assertEqual(result["best_day_rule"]["ratio"], "0.5")
        self.assertTrue(result["best_day_rule"]["compliant"])
        self.assertTrue(result["eligible"])

    def test_one_step_best_day_ratio_can_delay_eligibility(self):
        result = qualification_snapshot(
            config=self.config,
            account=_account(
                account_type=AccountType.ONE_STEP,
                phase=AccountPhase.EVALUATION,
            ),
            closed_trades=_trades("7000", "3000", "2000"),
            trading_day_events=[],
            history_status=_history_status(AccountPhase.EVALUATION),
        )
        self.assertFalse(result["best_day_rule"]["compliant"])
        self.assertFalse(result["eligible"])

    def test_one_step_funded_cycle_needs_positive_days_profit(self):
        result = qualification_snapshot(
            config=self.config,
            account=_account(
                account_type=AccountType.ONE_STEP,
                phase=AccountPhase.FTMO_ACCOUNT,
            ),
            closed_trades=[],
            trading_day_events=[],
            history_status=_history_status(AccountPhase.FTMO_ACCOUNT),
        )
        self.assertFalse(result["best_day_rule"]["compliant"])
        self.assertFalse(result["eligible"])

    def test_two_step_evaluation_requires_four_days_and_ten_percent(self):
        result = qualification_snapshot(
            config=self.config,
            account=_account(
                account_type=AccountType.TWO_STEP,
                phase=AccountPhase.EVALUATION,
            ),
            closed_trades=_trades("2500", "2500", "2500", "2500"),
            trading_day_events=_trading_days(4),
            history_status=_history_status(AccountPhase.EVALUATION),
        )
        self.assertEqual(result["minimum_trading_days"]["completed"], 4)
        self.assertTrue(result["minimum_trading_days"]["met"])
        self.assertEqual(result["profit_target"]["target_amount"], "10000.00")
        self.assertTrue(result["eligible"])

    def test_two_step_verification_uses_five_percent_target(self):
        result = qualification_snapshot(
            config=self.config,
            account=_account(
                account_type=AccountType.TWO_STEP,
                phase=AccountPhase.VERIFICATION,
            ),
            closed_trades=_trades("1250", "1250", "1250", "1250"),
            trading_day_events=_trading_days(4),
            history_status=_history_status(AccountPhase.VERIFICATION),
        )
        self.assertEqual(result["profit_target"]["target_amount"], "5000.00")
        self.assertTrue(result["eligible"])

    def test_minimum_days_come_from_open_events_not_closed_profit_days(self):
        result = qualification_snapshot(
            config=self.config,
            account=_account(
                account_type=AccountType.TWO_STEP,
                phase=AccountPhase.EVALUATION,
            ),
            closed_trades=_trades("10000"),
            trading_day_events=_trading_days(3),
            history_status=_history_status(AccountPhase.EVALUATION),
        )
        self.assertTrue(result["profit_target"]["met"])
        self.assertEqual(result["minimum_trading_days"]["completed"], 3)
        self.assertFalse(result["minimum_trading_days"]["met"])
        self.assertFalse(result["eligible"])

    def test_uncertain_history_requires_manual_review(self):
        result = qualification_snapshot(
            config=self.config,
            account=_account(
                account_type=AccountType.TWO_STEP,
                phase=AccountPhase.EVALUATION,
                uncertain=True,
            ),
            closed_trades=_trades("2500", "2500", "2500", "2500"),
            trading_day_events=_trading_days(4),
            history_status=_history_status(AccountPhase.EVALUATION),
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["qualification_status"], "uncertain")
        self.assertTrue(result["manual_review_required"])

    def test_missing_history_watermark_cannot_be_eligible(self):
        result = qualification_snapshot(
            config=self.config,
            account=_account(
                account_type=AccountType.TWO_STEP,
                phase=AccountPhase.EVALUATION,
            ),
            closed_trades=_trades("2500", "2500", "2500", "2500"),
            trading_day_events=_trading_days(4),
            history_status=None,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["qualification_status"], "uncertain")
        self.assertFalse(result["history"]["complete"])


if __name__ == "__main__":
    unittest.main()
