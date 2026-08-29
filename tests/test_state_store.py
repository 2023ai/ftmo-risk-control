import hashlib
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
from unittest.mock import patch

from src.risk_engine import (
    AccountPhase,
    AccountStyle,
    AccountType,
    RuleProfile,
    ftmo_day_key,
)
from src.state_store import CURRENT_SCHEMA_VERSION, StateStore


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

    def test_database_path_expands_user_and_is_absolute(self):
        self.store.close()
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            try:
                os.chdir(cwd)
                with patch.dict(os.environ, {"HOME": home}):
                    store = StateStore("~/.ftmo/risk.db")
                    try:
                        expected = Path(home) / ".ftmo" / "risk.db"
                        self.assertTrue(Path(store.path).is_absolute())
                        self.assertEqual(Path(store.path), Path(os.path.abspath(expected)))
                        self.assertTrue(expected.is_file())
                        self.assertFalse((Path(cwd) / "~").exists())
                    finally:
                        store.close()
            finally:
                os.chdir(original_cwd)

    def test_database_rejects_symbolic_link_parent_directory(self):
        self.store.close()
        real_parent = Path(self.tempdir.name) / "real-state"
        real_parent.mkdir()
        linked_parent = Path(self.tempdir.name) / "linked-state"
        os.symlink(real_parent, linked_parent)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            StateStore(linked_parent / "risk.db")
        self.assertEqual(list(real_parent.iterdir()), [])

    def test_database_integrity_scan_is_cached_for_one_minute(self):
        with patch(
            "src.state_store.time.monotonic",
            side_effect=(100.0, 110.0, 161.0),
        ):
            self.assertTrue(self.store.database_healthy())
            self.assertEqual(
                self.store._last_integrity_check_monotonic,
                100.0,
            )
            self.assertTrue(self.store.database_healthy())
            self.assertEqual(
                self.store._last_integrity_check_monotonic,
                100.0,
            )
            self.assertTrue(self.store.database_healthy())
            self.assertEqual(
                self.store._last_integrity_check_monotonic,
                161.0,
            )

    def test_current_schema_missing_required_table_is_rejected(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE risk_reservations")
            connection.execute(
                f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}"
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "risk_reservations"):
            StateStore(self.path)

    def test_current_schema_missing_required_column_is_rejected(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE accounts")
            connection.execute(
                "CREATE TABLE accounts (account_id TEXT PRIMARY KEY)"
            )
            connection.execute(
                f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}"
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "accounts.account_type"):
            StateStore(self.path)

    def test_newer_schema_version_is_rejected(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}"
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "newer than this service"):
            StateStore(self.path)

    def test_database_health_rejects_schema_version_tampering(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 0")
            connection.commit()
        self.assertFalse(self.store.database_healthy())

    def test_backup_rejects_unhealthy_schema(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version = 0")
            connection.commit()
        output = Path(self.tempdir.name) / "backups" / "unhealthy.db"
        with self.assertRaisesRegex(ValueError, "not healthy"):
            self.store.backup_to(output)
        self.assertFalse(output.exists())

    def test_rule_config_fingerprint_cannot_change_within_one_version(self):
        first = "a" * 64
        second = "b" * 64

        self.assertTrue(
            self.store.pin_rule_config_fingerprint(
                rule_version="test-rules-v1",
                config_fingerprint=first,
                now=self.now,
            )
        )
        self.assertTrue(
            self.store.pin_rule_config_fingerprint(
                rule_version="test-rules-v1",
                config_fingerprint=first,
                now=self.now + timedelta(seconds=1),
            )
        )
        self.assertFalse(
            self.store.pin_rule_config_fingerprint(
                rule_version="test-rules-v1",
                config_fingerprint=second,
                now=self.now + timedelta(seconds=2),
            )
        )
        self.assertTrue(
            self.store.pin_rule_config_fingerprint(
                rule_version="test-rules-v2",
                config_fingerprint=second,
                now=self.now + timedelta(seconds=3),
            )
        )

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
            coverage_start=fetched_at - timedelta(days=1),
            coverage_end=fetched_at + timedelta(days=7),
            payload=payload,
            rule_version="test-rule",
        )
        snapshot = StateStore(self.path).get_calendar_snapshots()["news"]
        self.assertEqual(snapshot.payload, payload)
        self.assertEqual(snapshot.rule_version, "test-rule")
        self.assertEqual(
            snapshot.coverage_start,
            fetched_at - timedelta(days=1),
        )
        self.assertEqual(
            snapshot.coverage_end,
            fetched_at + timedelta(days=7),
        )

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

    def test_calendar_snapshot_detects_persisted_payload_corruption(self):
        fetched_at = self.now.replace(microsecond=0)
        self.store.save_calendar_snapshot(
            calendar_type="news",
            fetched_at=fetched_at,
            coverage_start=fetched_at - timedelta(days=1),
            coverage_end=fetched_at + timedelta(days=7),
            payload=[],
            rule_version="test-rule",
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE calendar_snapshots
                SET payload_json = ?
                WHERE calendar_type = 'news'
                """,
                ('[{"event_id":"tampered"}]',),
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "content hash"):
            self.store.get_calendar_snapshots()

    def test_calendar_snapshot_detects_coverage_tampering(self):
        fetched_at = self.now.replace(microsecond=0)
        self.store.save_calendar_snapshot(
            calendar_type="market",
            fetched_at=fetched_at,
            coverage_start=fetched_at - timedelta(days=1),
            coverage_end=fetched_at + timedelta(days=7),
            payload=[],
            rule_version="test-rule",
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE calendar_snapshots
                SET coverage_end = ?
                WHERE calendar_type = 'market'
                """,
                ((fetched_at + timedelta(days=30)).isoformat(),),
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "safety metadata"):
            self.store.get_calendar_snapshots()

    def test_legacy_hash_version_cannot_claim_trusted_coverage(self):
        fetched_at = self.now.replace(microsecond=0)
        self.store.save_calendar_snapshot(
            calendar_type="news",
            fetched_at=fetched_at,
            coverage_start=fetched_at - timedelta(days=1),
            coverage_end=fetched_at + timedelta(days=7),
            payload=[],
            rule_version="test-rule",
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE calendar_snapshots
                SET hash_version = 1,
                    content_hash = ?
                WHERE calendar_type = 'news'
                """,
                (hashlib.sha256(b"[]").hexdigest(),),
            )
            connection.commit()
        with self.assertRaisesRegex(ValueError, "legacy calendar snapshots"):
            self.store.get_calendar_snapshots()

    def test_legacy_calendar_schema_migrates_without_trusting_coverage(self):
        legacy_path = Path(self.tempdir.name) / "legacy.db"
        payload_json = "[]"
        fetched_at = self.now.replace(microsecond=0)
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute(
                """
                CREATE TABLE calendar_snapshots (
                    calendar_type TEXT PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO calendar_snapshots (
                    calendar_type, fetched_at, content_hash, payload_json,
                    rule_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "news",
                    fetched_at.isoformat(),
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    payload_json,
                    "legacy-rule",
                    fetched_at.isoformat(),
                ),
            )
            connection.commit()

        migrated = StateStore(legacy_path).get_calendar_snapshots()["news"]
        self.assertEqual(migrated.payload, [])
        self.assertIsNone(migrated.coverage_start)
        self.assertIsNone(migrated.coverage_end)
        with closing(sqlite3.connect(legacy_path)) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(calendar_snapshots)"
                )
            }
        self.assertIn("coverage_start", columns)
        self.assertIn("coverage_end", columns)
        self.assertIn("hash_version", columns)

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
        first_last_used = authenticated.last_used_at
        authenticated_again = self.store.authenticate_account_credential(
            account_id="mt5-credential",
            secret=secret,
            scope="trade:evaluate",
            now=self.now + timedelta(seconds=30),
        )
        self.assertIsNotNone(authenticated_again)
        self.assertEqual(authenticated_again.last_used_at, first_last_used)
        authenticated_after_interval = (
            self.store.authenticate_account_credential(
                account_id="mt5-credential",
                secret=secret,
                scope="trade:evaluate",
                now=self.now + timedelta(seconds=62),
            )
        )
        self.assertIsNotNone(authenticated_after_interval)
        self.assertEqual(
            authenticated_after_interval.last_used_at,
            self.now + timedelta(seconds=62),
        )
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

    def test_credential_timestamp_inputs_must_be_timezone_aware(self):
        self.sync()
        naive = datetime(2026, 8, 29, 12, 0, 0)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.store.create_account_credential(
                account_id="mt5-10001",
                scopes=("trade:evaluate",),
                not_before=naive,
                expires_at=self.now + timedelta(hours=1),
                now=self.now,
            )
        record, _ = self.store.create_account_credential(
            account_id="mt5-10001",
            scopes=("trade:evaluate",),
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            now=self.now,
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.store.revoke_account_credential(
                record.credential_id,
                revoked_at=naive,
            )
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="mt5-10001",
                secret="invalid",
                scope="trade:evaluate",
                now=naive,
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

    def test_credential_revocation_never_moves_backwards(self):
        record, secret = self.store.create_account_credential(
            account_id="revocation-account",
            scopes=("trade:evaluate",),
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            now=self.now,
        )
        self.store.revoke_account_credential(
            record.credential_id,
            revoked_at=self.now + timedelta(minutes=10),
        )
        self.store.revoke_account_credential(
            record.credential_id,
            revoked_at=self.now + timedelta(minutes=1),
        )
        self.assertIsNotNone(
            self.store.authenticate_account_credential(
                account_id="revocation-account",
                secret=secret,
                scope="trade:evaluate",
                now=self.now + timedelta(minutes=5),
            )
        )
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="revocation-account",
                secret=secret,
                scope="trade:evaluate",
                now=self.now + timedelta(minutes=11),
            )
        )

    def test_rotation_never_shortens_a_future_revocation(self):
        record, secret = self.store.create_account_credential(
            account_id="rotation-revocation-account",
            scopes=("trade:evaluate",),
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            now=self.now,
        )
        self.store.revoke_account_credential(
            record.credential_id,
            revoked_at=self.now + timedelta(minutes=10),
        )
        self.store.rotate_account_credential(
            credential_id=record.credential_id,
            scopes=None,
            not_before=self.now + timedelta(minutes=5),
            expires_at=self.now + timedelta(hours=1),
            overlap_seconds=0,
            now=self.now + timedelta(minutes=5),
        )
        self.assertIsNotNone(
            self.store.authenticate_account_credential(
                account_id="rotation-revocation-account",
                secret=secret,
                scope="trade:evaluate",
                now=self.now + timedelta(minutes=9),
            )
        )
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="rotation-revocation-account",
                secret=secret,
                scope="trade:evaluate",
                now=self.now + timedelta(minutes=10),
            )
        )

    def test_rotation_does_not_reactivate_an_already_revoked_credential(self):
        record, secret = self.store.create_account_credential(
            account_id="already-revoked-account",
            scopes=("trade:evaluate",),
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            now=self.now,
        )
        self.store.revoke_account_credential(
            record.credential_id,
            revoked_at=self.now - timedelta(seconds=1),
        )
        self.store.rotate_account_credential(
            credential_id=record.credential_id,
            scopes=None,
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            overlap_seconds=60,
            now=self.now,
        )
        self.assertIsNone(
            self.store.authenticate_account_credential(
                account_id="already-revoked-account",
                secret=secret,
                scope="trade:evaluate",
                now=self.now,
            )
        )

    def test_rotation_rejects_explicit_empty_scope_list(self):
        record, _ = self.store.create_account_credential(
            account_id="empty-rotation-scope",
            scopes=("trade:evaluate",),
            not_before=self.now,
            expires_at=self.now + timedelta(hours=1),
            now=self.now,
        )
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.store.rotate_account_credential(
                credential_id=record.credential_id,
                scopes=[],
                not_before=self.now,
                expires_at=self.now + timedelta(hours=1),
                now=self.now,
            )

    def test_integrity_audit_detects_missing_allowed_open_reservation(self):
        self.sync()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                INSERT INTO decisions (
                    account_id, request_id, request_hash, action, symbol,
                    allowed, reservation_risk, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "mt5-10001",
                    "orphan-allowed-open",
                    "a" * 64,
                    "open",
                    "EURUSD",
                    1,
                    "100",
                    json.dumps(
                        {
                            "ok": True,
                            "decision": {
                                "code": "ALLOW",
                                "allowed": True,
                            },
                        }
                    ),
                    self.now.isoformat(),
                ),
            )
            connection.commit()
        issues = self.store.risk_state_integrity_issues()
        self.assertTrue(
            any("has no execution reservation" in item for item in issues)
        )

    def test_integrity_audit_detects_reservation_risk_mismatch(self):
        self.sync()
        self.store.evaluate_and_reserve(
            account_id="mt5-10001",
            request_id="risk-mismatch",
            request_hash="b" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=self.now,
            evaluator=lambda account, frequency: {
                "ok": True,
                "decision": {
                    "code": "ALLOW",
                    "allowed": True,
                    "reasons": [],
                },
            },
            reservation_risk=Decimal("100"),
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE decisions SET reservation_risk = '200' "
                "WHERE account_id = 'mt5-10001' "
                "AND request_id = 'risk-mismatch'"
            )
            connection.commit()
        issues = self.store.risk_state_integrity_issues()
        self.assertTrue(
            any("does not match its reservation risk" in item for item in issues)
        )

    def test_rejected_risk_increasing_decision_does_not_fail_integrity_audit(self):
        self.sync()
        response = self.store.evaluate_and_reserve(
            account_id="mt5-10001",
            request_id="rejected-risk-request",
            request_hash="c" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=self.now,
            evaluator=lambda account, frequency: {
                "ok": True,
                "decision": {
                    "code": "REJECT_UNKNOWN_EXECUTION",
                    "allowed": False,
                    "reasons": ["unknown execution"],
                },
            },
            reservation_risk=Decimal("100"),
        )
        self.assertFalse(response["decision"]["allowed"])
        self.assertEqual(self.store.risk_state_integrity_issues(), [])

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

    def test_backup_rejects_symbolic_link_destination(self):
        self.sync()
        outside = Path(self.tempdir.name) / "outside.db"
        outside.write_bytes(b"do not replace")
        destination = Path(self.tempdir.name) / "backup-link.db"
        os.symlink(outside, destination)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.store.backup_to(destination)
        self.assertEqual(outside.read_bytes(), b"do not replace")

    def test_backup_rejects_symbolic_link_parent_directory(self):
        self.sync()
        real_parent = Path(self.tempdir.name) / "real-parent"
        real_parent.mkdir()
        linked_parent = Path(self.tempdir.name) / "linked-parent"
        os.symlink(real_parent, linked_parent)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.store.backup_to(linked_parent / "backup.db")
        self.assertEqual(list(real_parent.iterdir()), [])

    def test_backup_restores_existing_sidecars_when_replace_fails(self):
        self.sync()
        output = Path(self.tempdir.name) / "backups" / "rollback.db"
        output.parent.mkdir()
        output.write_bytes(b"old database")
        wal = Path(str(output) + "-wal")
        shm = Path(str(output) + "-shm")
        wal.write_bytes(b"old wal")
        shm.write_bytes(b"old shm")
        real_replace = os.replace

        def replace_or_fail(source, destination):
            if Path(destination) == output:
                raise OSError("simulated atomic replace failure")
            return real_replace(source, destination)

        with patch("src.state_store.os.replace", side_effect=replace_or_fail):
            with self.assertRaisesRegex(OSError, "atomic replace"):
                self.store.backup_to(output)
        self.assertEqual(output.read_bytes(), b"old database")
        self.assertEqual(wal.read_bytes(), b"old wal")
        self.assertEqual(shm.read_bytes(), b"old shm")
        self.assertEqual(
            list(output.parent.glob(f".{output.name}.*.tmp")),
            [],
        )

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

    def test_backup_telemetry_failure_does_not_hide_installed_backup(self):
        self.sync()
        output = Path(self.tempdir.name) / "backups" / "telemetry.db"
        with patch.object(
            self.store,
            "record_backup_event",
            side_effect=RuntimeError("telemetry unavailable"),
        ):
            result = self.store.backup_to(output)
        self.assertEqual(result, output.resolve())
        self.assertTrue(output.is_file())

    def test_backup_temp_file_is_cleaned_when_install_fails(self):
        self.sync()
        output = Path(self.tempdir.name) / "backups" / "setup.db"
        with patch(
            "src.state_store.os.replace",
            side_effect=OSError("no replace"),
        ):
            with self.assertRaises(OSError):
                self.store.backup_to(output)
        leftovers = list(output.parent.glob(f".{output.name}.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_database_health_fails_closed_for_unsafe_sidecar(self):
        self.sync()
        self.store.close()
        outside = Path(self.tempdir.name) / "outside-wal"
        outside.write_text("not sqlite wal", encoding="ascii")
        sidecar = Path(str(self.path) + "-wal")
        sidecar.unlink(missing_ok=True)
        os.symlink(outside, sidecar)
        self.assertFalse(self.store.database_healthy())

    def test_normal_execution_state_has_no_integrity_issues(self):
        self.sync()
        self.assertEqual(self.store.risk_state_integrity_issues(), [])

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

    def test_two_step_phase_cannot_skip_verification(self):
        self.sync(account_type=AccountType.TWO_STEP)
        with self.assertRaisesRegex(ValueError, "skip verification"):
            self.sync(
                account_type=AccountType.TWO_STEP,
                phase=AccountPhase.FTMO_ACCOUNT,
                bootstrap_day_start_balance=None,
                bootstrap_highest_settled_balance=None,
            )

    def test_expired_pending_reservation_becomes_unknown(self):
        self.sync()
        self.store.evaluate_and_reserve(
            account_id="mt5-10001",
            request_id="lease-r1",
            request_hash="a" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=self.now,
            evaluator=lambda account, frequency: {
                "ok": True,
                "decision": {
                    "code": "ALLOW",
                    "allowed": True,
                    "reasons": [],
                },
            },
            block_on_unknown_execution=True,
            reservation_risk=Decimal("100"),
        )
        expired_at = (
            datetime.now(UTC) - timedelta(minutes=2)
        ).isoformat()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE risk_reservations
                SET created_at = ?, updated_at = ?
                WHERE account_id = 'mt5-10001' AND request_id = 'lease-r1'
                """,
                (expired_at, expired_at),
            )
            connection.commit()

        self.assertEqual(
            self.store.reconcile_expired_reservations("mt5-10001"),
            1,
        )
        self.assertEqual(self.store.unknown_execution_count("mt5-10001"), 1)
        reservation = self.store.list_risk_reservations(
            "mt5-10001",
            unresolved_only=True,
        )
        self.assertEqual(reservation[0]["status"], "unknown")
        resolved = self.store.record_execution(
            account_id="mt5-10001",
            request_id="lease-r1",
            request_hash="b" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=datetime.now(UTC),
            outcome="failure",
            detail="reconciled as rejected",
        )
        self.assertTrue(resolved["reservation_released"])
        self.assertEqual(self.store.unknown_execution_count("mt5-10001"), 0)

    def test_late_unknown_report_after_lease_expiry_remains_idempotently_locked(self):
        self.sync()
        self.store.evaluate_and_reserve(
            account_id="mt5-10001",
            request_id="late-unknown-r1",
            request_hash="a" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=self.now,
            evaluator=lambda account, frequency: {
                "ok": True,
                "decision": {
                    "code": "ALLOW",
                    "allowed": True,
                    "reasons": [],
                },
            },
            block_on_unknown_execution=True,
            reservation_risk=Decimal("100"),
        )
        expired_at = (
            datetime.now(UTC) - timedelta(minutes=2)
        ).isoformat()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE risk_reservations
                SET created_at = ?, updated_at = ?
                WHERE account_id = 'mt5-10001' AND request_id = 'late-unknown-r1'
                """,
                (expired_at, expired_at),
            )
            connection.commit()
        self.assertEqual(
            self.store.reconcile_expired_reservations("mt5-10001"),
            1,
        )

        late = self.store.record_execution(
            account_id="mt5-10001",
            request_id="late-unknown-r1",
            request_hash="b" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=datetime.now(UTC),
            outcome="unknown",
            detail="late timeout with a different diagnostic status",
        )
        self.assertEqual(late["outcome"], "unknown")
        self.assertFalse(late["reservation_released"])
        self.assertEqual(
            self.store.unknown_execution_count("mt5-10001"),
            1,
        )
        self.assertEqual(
            self.store.list_risk_reservations(
                "mt5-10001",
                unresolved_only=True,
            )[0]["status"],
            "unknown",
        )

    def test_account_read_reconciles_materialized_reservation_cache(self):
        self.sync()
        self.store.evaluate_and_reserve(
            account_id="mt5-10001",
            request_id="cache-r1",
            request_hash="e" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=self.now,
            evaluator=lambda account, frequency: {
                "ok": True,
                "decision": {
                    "code": "ALLOW",
                    "allowed": True,
                    "reasons": [],
                },
            },
            reservation_risk=Decimal("100"),
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE accounts SET reserved_open_risk = '0' "
                "WHERE account_id = 'mt5-10001'"
            )
            connection.commit()
        account = self.store.get_account("mt5-10001")
        self.assertEqual(
            account.snapshot.reserved_open_risk,
            Decimal("100"),
        )

    def test_stale_sync_cannot_release_committed_reservation(self):
        self.sync()
        self.store.evaluate_and_reserve(
            account_id="mt5-10001",
            request_id="ordering-r1",
            request_hash="c" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=self.now,
            evaluator=lambda account, frequency: {
                "ok": True,
                "decision": {
                    "code": "ALLOW",
                    "allowed": True,
                    "reasons": [],
                },
            },
            reservation_risk=Decimal("100"),
        )
        self.store.record_execution(
            account_id="mt5-10001",
            request_id="ordering-r1",
            request_hash="d" * 64,
            action="open",
            symbol="EURUSD",
            occurred_at=datetime.now(UTC),
            outcome="success",
            detail="filled",
        )
        with closing(sqlite3.connect(self.path)) as connection:
            execution_at = connection.execute(
                """
                SELECT created_at FROM executions
                WHERE account_id = 'mt5-10001' AND request_id = 'ordering-r1'
                """
            ).fetchone()[0]
            stored_as_of = (
                datetime.fromisoformat(execution_at) + timedelta(seconds=10)
            ).isoformat()
            connection.execute(
                """
                UPDATE accounts SET as_of = ? WHERE account_id = 'mt5-10001'
                """,
                (stored_as_of,),
            )
            connection.commit()
        stale_as_of = datetime.fromisoformat(execution_at) + timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "move backwards"):
            self.store.sync_account(
                account_id="mt5-10001",
                account_type=AccountType.ONE_STEP,
                phase=AccountPhase.EVALUATION,
                style=AccountStyle.STANDARD,
                initial_capital=Decimal("100000"),
                balance=Decimal("100000"),
                equity=Decimal("100000"),
                current_open_risk=Decimal("0"),
                as_of=stale_as_of,
                received_at=datetime.now(UTC),
            )
        self.assertEqual(
            self.store.risk_reservation_metrics("mt5-10001")["committed"],
            1,
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
