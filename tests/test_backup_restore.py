import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from scripts.restore_state import restore
from src.risk_api import make_server
from src.risk_engine import AccountPhase, AccountStyle, AccountType
from src.state_store import StateStore


UTC = timezone.utc


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)
        self.source = self.directory / "source.db"
        self.backup = self.directory / "backup.db"
        self.destination = self.directory / "destination.db"
        store = StateStore(self.source)
        now = datetime.now(UTC)
        store.sync_account(
            account_id="restore-account",
            account_type=AccountType.TWO_STEP,
            phase=AccountPhase.EVALUATION,
            style=AccountStyle.STANDARD,
            initial_capital=Decimal("100000"),
            balance=Decimal("101000"),
            equity=Decimal("101000"),
            current_open_risk=Decimal("0"),
            as_of=now,
            received_at=now,
            bootstrap_day_start_balance=Decimal("100000"),
            bootstrap_highest_settled_balance=Decimal("100000"),
        )
        store.save_calendar_snapshot(
            calendar_type="news",
            fetched_at=now,
            payload=[],
            rule_version="restore-test",
        )
        store.backup_to(self.backup)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_restore_replaces_state_with_checked_owner_only_copy(self):
        Path(str(self.destination) + "-wal").write_text(
            "stale",
            encoding="ascii",
        )
        Path(str(self.destination) + "-shm").write_text(
            "stale",
            encoding="ascii",
        )
        restored_path = restore(self.backup, self.destination)
        self.assertEqual(restored_path, self.destination.resolve())
        self.assertEqual(
            stat.S_IMODE(os.stat(restored_path).st_mode),
            0o600,
        )
        restored = StateStore(restored_path)
        self.assertEqual(
            restored.get_account("restore-account").snapshot.balance,
            Decimal("101000"),
        )
        self.assertIn("news", restored.get_calendar_snapshots())
        self.assertEqual(
            restored.backup_metrics()["restore_success"],
            1,
        )
        self.assertFalse(Path(str(self.destination) + "-wal").exists())
        self.assertFalse(Path(str(self.destination) + "-shm").exists())

    def test_restore_refuses_active_server_state(self):
        server = make_server(
            "127.0.0.1",
            0,
            "config/ftmo-v2.json",
            auth_token="admin-token",
            state_path=self.destination,
        )
        try:
            with self.assertRaises(RuntimeError):
                restore(self.backup, self.destination)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
