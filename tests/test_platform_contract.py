import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlatformContractTests(unittest.TestCase):
    def test_mt5_unreadable_position_fails_closed_in_open_risk_total(self):
        source = (ROOT / "platform" / "mt5" / "RiskGuardEA.mq5").read_text(
            encoding="utf-8"
        )
        start = source.index("double CurrentOpenRisk()")
        end = source.index("bool SyncAccount()", start)
        risk_function = source[start:end]
        self.assertIn(
            "// An unreadable position must never disappear from the risk total.",
            risk_function,
        )
        self.assertIn(
            "return InitialCapital;",
            risk_function,
        )

    def test_mt5_does_not_clear_local_lock_while_reconciliation_is_incomplete(self):
        source = (ROOT / "platform" / "mt5" / "RiskGuardEA.mq5").read_text(
            encoding="utf-8"
        )
        keep_lock = source.index(
            'if(JsonFalse(response, "reconciliation_complete"))'
        )
        clear_lock = source.index(
            'if(JsonFalse(response, "risk_increase_blocked"))'
        )
        self.assertLess(keep_lock, clear_lock)

    def test_ctrader_requires_complete_reconciliation_before_clearing_lock(self):
        source = (ROOT / "platform" / "ctrader" / "RiskGuardBot.cs").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "if (blocked || !reconciliationComplete)",
            source,
        )
        self.assertIn(
            'GetProperty("reconciliation_complete")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
