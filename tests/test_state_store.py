import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.risk_engine import (
    AccountPhase,
    AccountStyle,
    AccountType,
    ftmo_day_key,
)
from src.state_store import StateStore


UTC = timezone.utc


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "risk.db"
        self.store = StateStore(self.path)
        self.now = datetime.now(UTC)

    def tearDown(self):
        self.tempdir.cleanup()

    def sync(self, **overrides):
        values = {
            "account_id": "mt5-10001",
            "account_type": AccountType.ONE_STEP,
            "phase": AccountPhase.EVALUATION,
            "style": AccountStyle.STANDARD,
            "initial_capital": Decimal("100000"),
            "balance": Decimal("100000"),
            "equity": Decimal("99800"),
            "current_open_risk": Decimal("100"),
            "as_of": self.now,
            "bootstrap_day_start_balance": Decimal("100000"),
            "bootstrap_highest_settled_balance": Decimal("100000"),
        }
        values.update(overrides)
        return self.store.sync_account(**values)

    def test_first_sync_requires_verified_baselines(self):
        with self.assertRaises(ValueError):
            self.sync(
                bootstrap_day_start_balance=None,
                bootstrap_highest_settled_balance=None,
            )

    def test_account_survives_new_store_instance(self):
        self.sync()
        reloaded = StateStore(self.path).get_account("mt5-10001")
        self.assertEqual(
            reloaded.snapshot.day_start_balance,
            Decimal("100000"),
        )
        self.assertEqual(reloaded.snapshot.equity, Decimal("99800"))

    def test_one_step_rollover_updates_settled_high_water_mark(self):
        self.sync()
        self.sync(
            balance=Decimal("103000"),
            equity=Decimal("103000"),
        )
        next_day = self.now + timedelta(days=1)
        self.store.confirm_settlement(
            account_id="mt5-10001",
            ftmo_day=ftmo_day_key(next_day),
            settled_balance=Decimal("103000"),
            settled_at=next_day,
            source="test-schedule",
        )
        account = self.sync(
            balance=Decimal("104000"),
            equity=Decimal("104000"),
            as_of=next_day,
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertEqual(
            account.snapshot.day_start_balance,
            Decimal("103000"),
        )
        self.assertEqual(
            account.snapshot.highest_settled_balance,
            Decimal("103000"),
        )
        self.assertFalse(account.snapshot.data_uncertain)

    def test_two_step_rollover_does_not_trail_high_water_mark(self):
        self.sync(
            account_id="ctrader-20002",
            account_type=AccountType.TWO_STEP,
        )
        self.sync(
            account_id="ctrader-20002",
            account_type=AccountType.TWO_STEP,
            balance=Decimal("103000"),
            equity=Decimal("103000"),
        )
        account = self.sync(
            account_id="ctrader-20002",
            account_type=AccountType.TWO_STEP,
            balance=Decimal("104000"),
            equity=Decimal("104000"),
            as_of=self.now + timedelta(days=1),
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertEqual(
            account.snapshot.highest_settled_balance,
            Decimal("100000"),
        )
        self.assertTrue(account.snapshot.data_uncertain)

    def test_missing_settlement_marks_new_day_uncertain(self):
        self.sync()
        next_day = self.now + timedelta(days=1)
        account = self.sync(
            balance=Decimal("101000"),
            equity=Decimal("101000"),
            as_of=next_day,
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertTrue(account.snapshot.data_uncertain)

    def test_frequency_survives_restart(self):
        self.sync()
        self.store.record_activity(
            account_id="mt5-10001",
            kind="request",
            symbol="EURUSD",
            occurred_at=self.now,
            request_id="r1",
        )
        self.store.record_activity(
            account_id="mt5-10001",
            kind="open",
            symbol="EURUSD",
            occurred_at=self.now,
            request_id="r1",
        )
        frequency = StateStore(self.path).frequency("mt5-10001")
        self.assertEqual(len(frequency.request_times), 1)
        self.assertEqual(len(frequency.open_times), 1)

    def test_style_cannot_change_after_bootstrap(self):
        self.sync()
        with self.assertRaises(ValueError):
            self.sync(
                style=AccountStyle.SWING,
                bootstrap_day_start_balance=None,
                bootstrap_highest_settled_balance=None,
            )

    def test_phase_can_only_advance(self):
        self.sync(phase=AccountPhase.FTMO_ACCOUNT)
        with self.assertRaises(ValueError):
            self.sync(
                phase=AccountPhase.EVALUATION,
                bootstrap_day_start_balance=None,
                bootstrap_highest_settled_balance=None,
            )

    def test_failed_execution_can_release_open_reservation(self):
        self.sync()
        self.store.record_activity(
            account_id="mt5-10001",
            kind="open",
            symbol="EURUSD",
            occurred_at=self.now,
            request_id="r1",
        )
        self.store.release_activity(
            account_id="mt5-10001",
            kind="open",
            request_id="r1",
        )
        self.assertEqual(
            len(self.store.frequency("mt5-10001").open_times),
            0,
        )


if __name__ == "__main__":
    unittest.main()
