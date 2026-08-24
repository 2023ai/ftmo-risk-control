import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
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

    def test_calendar_snapshot_survives_restart(self):
        fetched_at = self.now.replace(microsecond=0)
        payload = [
            {
                "event_id": "NFP",
                "release_time": fetched_at.isoformat(),
                "affected_symbols": ["EURUSD"],
                "importance": "high",
                "source": "test",
            }
        ]
        self.store.save_calendar_snapshot(
            calendar_type="news",
            fetched_at=fetched_at,
            payload=payload,
            rule_version="test-rule",
        )
        snapshot = StateStore(self.path).get_calendar_snapshots()["news"]
        self.assertEqual(snapshot.payload, payload)
        self.assertEqual(snapshot.rule_version, "test-rule")

    def test_calendar_snapshot_rejects_older_and_conflicting_content(self):
        fetched_at = self.now.replace(microsecond=0)
        self.store.save_calendar_snapshot(
            calendar_type="news",
            fetched_at=fetched_at,
            payload=[],
            rule_version="test-rule",
        )
        with self.assertRaises(ValueError):
            self.store.save_calendar_snapshot(
                calendar_type="news",
                fetched_at=fetched_at - timedelta(seconds=1),
                payload=[],
                rule_version="test-rule",
            )
        with self.assertRaises(ValueError):
            self.store.save_calendar_snapshot(
                calendar_type="news",
                fetched_at=fetched_at,
                payload=[{"event_id": "conflict"}],
                rule_version="test-rule",
            )
        with self.assertRaises(ValueError):
            self.store.save_calendar_snapshot(
                calendar_type="news",
                fetched_at=fetched_at,
                payload=[],
                rule_version="different-rule",
            )

    def test_account_credential_scope_expiry_and_revocation(self):
        record, secret = self.store.create_account_credential(
            account_id="mt5-credential",
            scopes=("trade:evaluate",),
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            now=self.now,
        )
        authenticated = self.store.authenticate_account_credential(
            account_id="mt5-credential",
            secret=secret,
            scope="trade:evaluate",
            now=self.now + timedelta(seconds=1),
        )
        self.assertIsNotNone(authenticated)
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="other-account",
                secret=secret,
                scope="trade:evaluate",
                now=self.now + timedelta(seconds=1),
            )
        )
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="mt5-credential",
                secret=secret,
                scope="trade:execution",
                now=self.now + timedelta(seconds=1),
            )
        )
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="mt5-credential",
                secret=secret,
                scope="trade:evaluate",
                now=self.now + timedelta(hours=2),
            )
        )
        self.store.revoke_account_credential(
            record.credential_id,
            revoked_at=self.now + timedelta(minutes=5),
        )
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="mt5-credential",
                secret=secret,
                scope="trade:evaluate",
                now=self.now + timedelta(minutes=6),
            )
        )

    def test_legacy_admin_wildcard_credential_is_not_authorized(self):
        record, secret = self.store.create_account_credential(
            account_id="legacy-wildcard",
            scopes=("trade:evaluate",),
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            now=self.now,
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE account_credentials
                SET scopes_json = ?
                WHERE credential_id = ?
                """,
                (json.dumps(["admin:*"]), record.credential_id),
            )
            connection.commit()
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="legacy-wildcard",
                secret=secret,
                scope="trade:evaluate",
                now=self.now,
            )
        )

    def test_credential_rotation_supports_bounded_overlap(self):
        old_record, old_secret = self.store.create_account_credential(
            account_id="rotation-account",
            scopes=("trade:evaluate",),
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            now=self.now,
        )
        new_record, new_secret = self.store.rotate_account_credential(
            credential_id=old_record.credential_id,
            scopes=None,
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            overlap_seconds=60,
            now=self.now,
        )
        self.assertNotEqual(old_record.credential_id, new_record.credential_id)
        self.assertIsNotNone(
            self.store.authenticate_account_credential(
                account_id="rotation-account",
                secret=old_secret,
                scope="trade:evaluate",
                now=self.now + timedelta(seconds=30),
            )
        )
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="rotation-account",
                secret=old_secret,
                scope="trade:evaluate",
                now=self.now + timedelta(seconds=61),
            )
        )
        self.assertIsNotNone(
            self.store.authenticate_account_credential(
                account_id="rotation-account",
                secret=new_secret,
                scope="trade:evaluate",
                now=self.now + timedelta(seconds=61),
            )
        )

    def test_online_backup_is_owner_only_and_readable(self):
        self.sync()
        output = Path(self.tempdir.name) / "backups" / "risk.db"
        result = self.store.backup_to(output)
        self.assertEqual(result, output.resolve())
        self.assertEqual(stat.S_IMODE(os.stat(result).st_mode), 0o600)
        restored = StateStore(result).get_account("mt5-10001")
        self.assertEqual(restored.snapshot.equity, Decimal("99800"))
        metrics = self.store.backup_metrics()
        self.assertEqual(metrics["backup_success"], 1)

    def test_backup_age_uses_last_success_after_failed_attempt(self):
        output = Path(self.tempdir.name) / "backups" / "risk.db"
        self.store.backup_to(output)
        self.store.record_backup_event(
            operation="backup",
            success=False,
            detail="simulated failure",
        )
        metrics = self.store.backup_metrics()
        self.assertIsNotNone(metrics["last_backup_at"])
        self.assertFalse(metrics["last_backup_success"])
        self.assertIsNotNone(metrics["last_backup_attempt_at"])

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
            "open_positions_count": 0,
            "pending_orders_count": 0,
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

    def test_open_position_inventory_survives_restart(self):
        self.sync(open_positions_count=2, pending_orders_count=3)
        reloaded = StateStore(self.path).get_account("mt5-10001")
        self.assertEqual(reloaded.snapshot.open_positions_count, 2)
        self.assertEqual(reloaded.snapshot.pending_orders_count, 3)

    def test_equal_account_snapshot_timestamp_rejects_conflicting_content(self):
        self.sync()
        with self.assertRaises(ValueError):
            self.sync(balance=Decimal("100001"))

    def test_in_memory_store_retains_state_between_operations(self):
        store = StateStore(":memory:")
        try:
            account = store.sync_account(
                account_id="memory-account",
                account_type=AccountType.TWO_STEP,
                phase=AccountPhase.EVALUATION,
                style=AccountStyle.STANDARD,
                initial_capital=Decimal("100000"),
                balance=Decimal("100000"),
                equity=Decimal("100000"),
                current_open_risk=Decimal("0"),
                open_positions_count=0,
                pending_orders_count=0,
                as_of=self.now,
                received_at=self.now,
                bootstrap_day_start_balance=Decimal("100000"),
                bootstrap_highest_settled_balance=Decimal("100000"),
            )
            self.assertEqual(
                store.get_account(account.account_id).snapshot.balance,
                Decimal("100000"),
            )
        finally:
            store.close()

    def test_one_step_rollover_updates_settled_high_water_mark(self):
        self.sync()
        self.sync(
            balance=Decimal("103000"),
            equity=Decimal("103000"),
            as_of=self.now + timedelta(seconds=1),
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
            as_of=self.now + timedelta(seconds=1),
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
            as_of=after_rollover,
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

    def test_settlement_day_must_match_settlement_timestamp(self):
        self.sync()
        settled_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
        mismatched_day = ftmo_day_key(settled_at + timedelta(days=1))
        self.assertNotEqual(ftmo_day_key(settled_at), mismatched_day)
        with self.assertRaises(ValueError):
            self.store.confirm_settlement(
                account_id="mt5-10001",
                ftmo_day=mismatched_day,
                settled_balance=Decimal("100000"),
                settled_at=settled_at,
                source="approved-source",
            )

    def test_qualification_history_cannot_get_ahead_of_account_phase(self):
        self.sync()
        with self.assertRaises(ValueError):
            self.store.record_closed_trade(
                account_id="mt5-10001",
                trade_id="future-phase-trade",
                phase=AccountPhase.VERIFICATION,
                cycle_id="verification-1",
                closed_at=self.now,
                ftmo_day=ftmo_day_key(self.now),
                net_profit=Decimal("100"),
                symbol="EURUSD",
                source="test",
                request_id="future-phase-request",
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

    def test_frequency_prunes_old_modification_cooldowns(self):
        self.sync()
        self.store.record_activity(
            account_id="mt5-10001",
            kind="modify",
            symbol="EURUSD",
            occurred_at=self.now - timedelta(days=3),
            request_id="old-modify",
        )
        frequency = self.store.frequency("mt5-10001")
        self.assertNotIn("EURUSD", frequency.last_modify_by_symbol)

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

    def test_two_step_phase_advances_through_verification(self):
        self.sync(account_type=AccountType.TWO_STEP)
        verified = self.sync(
            account_type=AccountType.TWO_STEP,
            phase=AccountPhase.VERIFICATION,
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertEqual(verified.phase, AccountPhase.VERIFICATION)
        funded = self.sync(
            account_type=AccountType.TWO_STEP,
            phase=AccountPhase.FTMO_ACCOUNT,
            bootstrap_day_start_balance=None,
            bootstrap_highest_settled_balance=None,
        )
        self.assertEqual(funded.phase, AccountPhase.FTMO_ACCOUNT)

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
