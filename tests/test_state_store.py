import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.risk_engine import (
    AccountPhase,
    AccountStyle,
    AccountType,
    RuleProfile,
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

    def test_database_permissions_are_owner_only(self):
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

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
        if "received_at" not in overrides:
            values["received_at"] = values["as_of"]
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

    def test_rollover_uses_server_received_day(self):
        before_rollover = datetime(2026, 8, 22, 21, 59, 59, tzinfo=UTC)
        after_rollover = before_rollover + timedelta(seconds=2)
        self.sync(
            as_of=before_rollover,
            received_at=before_rollover,
        )
        account = self.sync(
            balance=Decimal("101000"),
            equity=Decimal("101000"),
            as_of=before_rollover,
            received_at=after_rollover,
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertEqual(account.ftmo_day, ftmo_day_key(after_rollover))
        self.assertTrue(account.snapshot.data_uncertain)

    def test_daily_lock_persists_until_next_ftmo_day(self):
        profile = RuleProfile.two_step_default()
        account = self.sync(
            account_type=AccountType.TWO_STEP,
            equity=Decimal("95900"),
            profile=profile,
        )
        self.assertTrue(account.snapshot.day_locked)

        recovered_at = self.now + timedelta(seconds=1)
        account = self.sync(
            account_type=AccountType.TWO_STEP,
            equity=Decimal("100000"),
            as_of=recovered_at,
            received_at=recovered_at,
            profile=profile,
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertTrue(account.snapshot.day_locked)

        next_day = self.now + timedelta(days=1)
        account = self.sync(
            account_type=AccountType.TWO_STEP,
            equity=Decimal("100000"),
            as_of=next_day,
            received_at=next_day,
            profile=profile,
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertFalse(account.snapshot.day_locked)

    def test_official_breach_latch_survives_equity_recovery(self):
        profile = RuleProfile.two_step_default()
        account = self.sync(
            account_type=AccountType.TWO_STEP,
            equity=Decimal("94900"),
            profile=profile,
        )
        self.assertTrue(account.snapshot.breach_latched)

        recovered_at = self.now + timedelta(seconds=1)
        account = self.sync(
            account_type=AccountType.TWO_STEP,
            equity=Decimal("100000"),
            as_of=recovered_at,
            received_at=recovered_at,
            profile=profile,
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertTrue(account.snapshot.breach_latched)

    def test_confirmed_settlement_rejects_older_or_conflicting_record(self):
        self.sync()
        ftmo_day = ftmo_day_key(self.now)
        self.store.confirm_settlement(
            account_id="mt5-10001",
            ftmo_day=ftmo_day,
            settled_balance=Decimal("100500"),
            settled_at=self.now,
            source="approved-source",
        )
        with self.assertRaises(ValueError):
            self.store.confirm_settlement(
                account_id="mt5-10001",
                ftmo_day=ftmo_day,
                settled_balance=Decimal("100400"),
                settled_at=self.now - timedelta(seconds=1),
                source="approved-source",
            )
        with self.assertRaises(ValueError):
            self.store.confirm_settlement(
                account_id="mt5-10001",
                ftmo_day=ftmo_day,
                settled_balance=Decimal("100400"),
                settled_at=self.now,
                source="approved-source",
            )

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

    def test_future_activity_cannot_prune_other_account_frequency(self):
        self.sync()
        self.sync(
            account_id="ctrader-20002",
            account_type=AccountType.TWO_STEP,
        )
        self.store.record_activity(
            account_id="ctrader-20002",
            kind="request",
            symbol="EURUSD",
            occurred_at=self.now,
            request_id="other-current",
        )
        self.store.record_activity(
            account_id="mt5-10001",
            kind="execution",
            symbol="EURUSD",
            occurred_at=self.now + timedelta(days=10),
            request_id="future-event",
        )
        frequency = self.store.frequency("ctrader-20002")
        self.assertEqual(len(frequency.request_times), 1)

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
