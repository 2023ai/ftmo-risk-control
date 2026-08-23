import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.risk_api import make_server
from src.risk_engine import (
    AccountPhase,
    AccountStyle,
    AccountType,
    ftmo_day_key,
)


UTC = timezone.utc


def _snapshot(equity="100000"):
    return {
        "initial_capital": "100000",
        "day_start_balance": "100000",
        "highest_settled_balance": "100000",
        "balance": "100000",
        "equity": equity,
        "as_of": "2026-08-22T12:00:00+00:00",
        "current_open_risk": "0",
        "data_age_seconds": 0,
    }


def _open_request(when="2026-08-22T12:00:00+00:00"):
    return {
        "symbol": "EURUSD",
        "action": "open",
        "requested_at": when,
        "volume": "1",
        "entry_price": "1.1000",
        "stop_loss": "1.0800",
        "loss_per_volume_unit": "100",
        "estimated_costs": "8",
        "is_risk_increasing": True,
    }


class RiskAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(
            "127.0.0.1",
            0,
            "config/ftmo-v2.json",
            auth_token="test-token",
            allow_stateless_evaluate=True,
            allow_stateless_position_size=True,
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    def setUp(self):
        now = datetime.now(UTC).isoformat()
        self.request(
            "POST",
            "/v1/news-sync",
            {"fetched_at": now, "events": []},
        )
        self.request(
            "POST",
            "/v1/market-sync",
            {"fetched_at": now, "closures": []},
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, payload=None, token="test-token"):
        headers = {}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if token is not None:
            headers["X-Risk-Token"] = token
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_health_returns_rule_version(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["rule_version"], "ftmo-v2-2026-08-23")

    def test_evaluate_allows_order_within_budget(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": _open_request(),
            "news_data_age_seconds": 0,
            "market_data_age_seconds": 0,
        }
        status, body = self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["decision"]["allowed"])
        self.assertEqual(body["decision"]["code"], "ALLOW")
        self.assertEqual(
            Decimal(body["decision"]["risk_budget"]["total_budget"]),
            Decimal("250"),
        )

    def test_evaluate_rejects_hard_news_window(self):
        event_time = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
        self.request(
            "POST",
            "/v1/news-sync",
            {
                "fetched_at": datetime.now(UTC).isoformat(),
                "events": [
                    {
                        "event_id": "NFP",
                        "release_time": event_time.isoformat(),
                        "affected_symbols": ["EURUSD"],
                    }
                ],
            },
        )
        payload = {
            "account_type": "two_step",
            "phase": "ftmo_account",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": _open_request(
                (event_time + timedelta(minutes=1)).isoformat()
            ),
        }
        status, body = self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["code"], "REJECT_NEWS")
        self.assertFalse(body["decision"]["allowed"])

    def test_standard_account_rejects_missing_news_cache(self):
        with self.server.news_lock:
            self.server.news_fetched_at = datetime.now(UTC) - timedelta(
                seconds=61
            )
        payload = {
            "account_type": "two_step",
            "phase": "ftmo_account",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": _open_request(),
        }
        status, body = self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["code"], "REJECT_DATA_STALE")

    def test_news_sync_populates_cache_for_standard_account(self):
        event_time = datetime(2026, 8, 22, 12, 30, tzinfo=UTC)
        sync_payload = {
            "fetched_at": datetime.now(UTC).isoformat(),
            "events": [
                {
                    "event_id": "CPI",
                    "release_time": event_time.isoformat(),
                    "affected_symbols": ["EURUSD"],
                }
            ],
        }
        status, body = self.request("POST", "/v1/news-sync", sync_payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["event_count"], 1)

        payload = {
            "account_type": "two_step",
            "phase": "ftmo_account",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": _open_request(),
        }
        status, body = self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["code"], "ALLOW")

    def test_future_news_sync_timestamp_is_bad_request(self):
        payload = {
            "fetched_at": (
                datetime.now(UTC) + timedelta(minutes=5)
            ).isoformat(),
            "events": [],
        }
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/news-sync", payload)
        self.assertEqual(context.exception.code, 400)

    def test_position_size_returns_decimal_strings(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "loss_per_volume_unit": "100",
            "volume_step": "0.01",
            "min_volume": "0.01",
        }
        status, body = self.request("POST", "/v1/position-size", payload)
        self.assertEqual(status, 200)
        self.assertEqual(
            Decimal(body["position_size"]["volume"]),
            Decimal("2.50"),
        )
        self.assertEqual(
            Decimal(body["position_size"]["expected_loss"]),
            Decimal("250"),
        )

    def test_position_size_returns_zero_when_account_is_locked(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(equity="95999"),
            "loss_per_volume_unit": "100",
            "volume_step": "0.01",
            "min_volume": "0.01",
        }
        status, body = self.request("POST", "/v1/position-size", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["account_status"], "LOCKED")
        self.assertEqual(Decimal(body["position_size"]["volume"]), Decimal("0"))

    def test_invalid_timestamp_is_bad_request(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": {
                **_snapshot(),
                "as_of": "2026-08-22T12:00:00",
            },
            "request": _open_request(),
        }
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(context.exception.code, 400)

    def test_string_boolean_is_bad_request(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": {
                **_open_request(),
                "is_risk_increasing": "false",
            },
        }
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(context.exception.code, 400)

    def test_open_cannot_be_marked_as_risk_reducing(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": {
                **_open_request(),
                "is_risk_increasing": False,
            },
        }
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(context.exception.code, 400)

    def test_risk_increasing_modify_cannot_remove_stop_loss(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": {
                "symbol": "EURUSD",
                "action": "modify",
                "requested_at": "2026-08-22T12:00:00+00:00",
                "stop_loss": None,
                "additional_risk": "100",
                "is_risk_increasing": True,
            },
        }
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(context.exception.code, 400)

    def test_invalid_token_is_unauthorized(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": _open_request(),
        }
        with self.assertRaises(HTTPError) as context:
            self.request("POST", "/v1/evaluate", payload, token="wrong")
        self.assertEqual(context.exception.code, 401)

    def test_stateless_client_cannot_override_calendar_freshness(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": _open_request(),
            "news_data_age_seconds": 0,
            "market_data_age_seconds": 0,
            "news_events": [],
            "market_closures": [],
        }
        with self.server.news_lock:
            self.server.news_fetched_at = datetime.now(UTC) - timedelta(
                seconds=61
            )
        status, body = self.request("POST", "/v1/evaluate", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["code"], "REJECT_DATA_STALE")

    def test_older_calendar_update_cannot_overwrite_fresh_cache(self):
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-sync",
                {
                    "fetched_at": (
                        datetime.now(UTC) - timedelta(seconds=10)
                    ).isoformat(),
                    "events": [],
                },
            )
        self.assertEqual(context.exception.code, 400)

    def test_equal_calendar_timestamp_cannot_change_content(self):
        with self.server.news_lock:
            fetched_at = self.server.news_fetched_at
        self.assertIsNotNone(fetched_at)
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-sync",
                {
                    "fetched_at": fetched_at.isoformat(),
                    "events": [
                        {
                            "event_id": "CONFLICT",
                            "release_time": datetime.now(UTC).isoformat(),
                            "affected_symbols": ["EURUSD"],
                        }
                    ],
                },
            )
        self.assertEqual(context.exception.code, 400)

    def test_duplicate_calendar_ids_are_rejected(self):
        event = {
            "event_id": "DUPLICATE",
            "release_time": datetime.now(UTC).isoformat(),
            "affected_symbols": ["EURUSD"],
        }
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-sync",
                {
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "events": [event, event],
                },
            )
        self.assertEqual(context.exception.code, 400)


class SecurityContractTests(unittest.TestCase):
    def test_remote_bind_without_token_is_rejected(self):
        with self.assertRaises(ValueError):
            make_server(
                "0.0.0.0",
                0,
                "config/ftmo-v2.json",
                auth_token="",
            )

    def test_remote_bind_requires_explicit_opt_in_even_with_token(self):
        with self.assertRaises(ValueError):
            make_server(
                "0.0.0.0",
                0,
                "config/ftmo-v2.json",
                auth_token="test-token",
            )

    def test_remote_bind_can_be_explicitly_enabled(self):
        server = make_server(
            "0.0.0.0",
            0,
            "config/ftmo-v2.json",
            auth_token="test-token",
            allow_remote_bind=True,
        )
        server.server_close()

    def test_stateless_position_size_is_disabled_by_default(self):
        server = make_server(
            "127.0.0.1",
            0,
            "config/ftmo-v2.json",
            auth_token="test-token",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "loss_per_volume_unit": "100",
            "volume_step": "0.01",
            "min_volume": "0.01",
        }
        request = Request(
            f"http://{host}:{port}/v1/position-size",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Risk-Token": "test-token",
            },
            method="POST",
        )
        try:
            with self.assertRaises(HTTPError) as context:
                urlopen(request, timeout=2)
            self.assertEqual(context.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_server_pins_validated_config_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.json")
            with open("config/ftmo-v2.json", encoding="utf-8") as source:
                config = json.load(source)
            with open(config_path, "w", encoding="utf-8") as target:
                json.dump(config, target)
            server = make_server(
                "127.0.0.1",
                0,
                config_path,
                auth_token="test-token",
            )
            try:
                config["rule_version"] = "changed-without-restart"
                with open(config_path, "w", encoding="utf-8") as target:
                    json.dump(config, target)
                self.assertEqual(
                    server.config["rule_version"],
                    "ftmo-v2-2026-08-23",
                )
            finally:
                server.server_close()

    def test_audit_file_permissions_are_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(
                "127.0.0.1",
                0,
                "config/ftmo-v2.json",
                auth_token="test-token",
            )
            audit_path = os.path.join(directory, "audit.jsonl")
            server.audit_path = audit_path
            try:
                server.write_audit({"ok": True})
                mode = stat.S_IMODE(os.stat(audit_path).st_mode)
                self.assertEqual(mode, 0o600)
            finally:
                server.server_close()


class StatefulRiskAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.server = make_server(
            "127.0.0.1",
            0,
            "config/ftmo-v2.json",
            auth_token="test-token",
            state_path=f"{cls.tempdir.name}/risk.db",
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()

    def request(self, path, payload, request_id=None):
        headers = {
            "Content-Type": "application/json",
            "X-Risk-Token": "test-token",
        }
        if request_id:
            headers["X-Request-Id"] = request_id
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def sync_account(
        self,
        account_id="stateful-10001",
        phase="evaluation",
        style="standard",
    ):
        result = self.request(
            "/v1/account-sync",
            {
                "account_id": account_id,
                "account_type": "two_step",
                "phase": phase,
                "style": style,
                "initial_capital": "100000",
                "day_start_balance": "100000",
                "highest_settled_balance": "100000",
                "balance": "100000",
                "equity": "100000",
                "current_open_risk": "0",
                "as_of": datetime.now(UTC).isoformat(),
            },
        )
        now = datetime.now(UTC).isoformat()
        self.request(
            "/v1/news-sync",
            {"fetched_at": now, "events": []},
        )
        self.request(
            "/v1/market-sync",
            {"fetched_at": now, "closures": []},
        )
        return result

    def test_account_sync_and_stateful_evaluation(self):
        status, body = self.sync_account("stateful-sync")
        self.assertEqual(status, 200)
        self.assertEqual(body["account_id"], "stateful-sync")
        self.assertEqual(body["snapshot"]["day_start_balance"], "100000")

        status, body = self.request(
            "/v1/evaluate",
            {
                "account_id": "stateful-sync",
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            request_id="stateful-r1",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["code"], "ALLOW")
        self.assertEqual(body["request_id"], "stateful-r1")

    def test_observed_daily_lock_persists_after_equity_recovers(self):
        account_id = "stateful-daily-lock"
        now = datetime.now(UTC)
        base_payload = {
            "account_id": account_id,
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "initial_capital": "100000",
            "day_start_balance": "100000",
            "highest_settled_balance": "100000",
            "balance": "100000",
            "current_open_risk": "0",
        }
        _, locked = self.request(
            "/v1/account-sync",
            {
                **base_payload,
                "equity": "95900",
                "as_of": now.isoformat(),
            },
        )
        self.assertTrue(locked["snapshot"]["day_locked"])

        _, recovered = self.request(
            "/v1/account-sync",
            {
                **base_payload,
                "equity": "100000",
                "as_of": datetime.now(UTC).isoformat(),
            },
        )
        self.assertTrue(recovered["snapshot"]["day_locked"])
        sync_time = datetime.now(UTC).isoformat()
        self.request(
            "/v1/news-sync",
            {"fetched_at": sync_time, "events": []},
        )
        self.request(
            "/v1/market-sync",
            {"fetched_at": sync_time, "closures": []},
        )
        _, decision = self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            request_id="daily-lock-r1",
        )
        self.assertEqual(
            decision["decision"]["code"],
            "REJECT_INTERNAL_LOCK",
        )

    def test_failed_execution_releases_frequency_reservation(self):
        account_id = "stateful-release"
        self.sync_account(account_id)
        now = datetime.now(UTC)
        for index in range(3):
            _, body = self.request(
                "/v1/evaluate",
                {
                    "account_id": account_id,
                    "request": _open_request(
                        (now + timedelta(seconds=index)).isoformat()
                    ),
                },
                request_id=f"release-r{index}",
            )
            self.assertEqual(body["decision"]["code"], "ALLOW")

        _, body = self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(
                    (now + timedelta(seconds=4)).isoformat()
                ),
            },
            request_id="release-r4",
        )
        self.assertEqual(body["decision"]["code"], "REJECT_FREQUENCY")

        _, body = self.request(
            "/v1/execution-result",
            {
                "account_id": account_id,
                "request_id": "release-r0",
                "success": False,
                "action": "open",
                "symbol": "EURUSD",
                "occurred_at": datetime.now(UTC).isoformat(),
                "platform_status": "rejected",
            },
        )
        self.assertTrue(body["reservation_released"])

        _, body = self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(
                    (now + timedelta(seconds=5)).isoformat()
                ),
            },
            request_id="release-r5",
        )
        self.assertEqual(body["decision"]["code"], "ALLOW")

    def test_same_request_id_replays_the_original_decision(self):
        account_id = "stateful-idempotent"
        self.sync_account(account_id)
        request_id = "idempotent-r1"
        payload = {
            "account_id": account_id,
            "request": _open_request(datetime.now(UTC).isoformat()),
        }
        _, first = self.request(
            "/v1/evaluate",
            payload,
            request_id=request_id,
        )
        _, second = self.request(
            "/v1/evaluate",
            payload,
            request_id=request_id,
        )
        self.assertEqual(first["decision"], second["decision"])
        self.assertEqual(
            len(self.server.state_store.frequency(account_id).open_times),
            1,
        )

    def test_request_id_reuse_with_different_content_is_rejected(self):
        account_id = "stateful-idempotent-conflict"
        self.sync_account(account_id)
        request_id = "idempotent-conflict"
        self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            request_id=request_id,
        )
        with self.assertRaises(HTTPError) as context:
            self.request(
                "/v1/evaluate",
                {
                    "account_id": account_id,
                    "request": {
                        **_open_request(datetime.now(UTC).isoformat()),
                        "volume": "2",
                    },
                },
                request_id=request_id,
            )
        self.assertEqual(context.exception.code, 400)

    def test_concurrent_evaluations_respect_five_minute_limit(self):
        account_id = "stateful-concurrent"
        self.sync_account(account_id)
        now = datetime.now(UTC).isoformat()

        def evaluate(index):
            return self.request(
                "/v1/evaluate",
                {
                    "account_id": account_id,
                    "request": _open_request(now),
                },
                request_id=f"concurrent-{index}",
            )[1]["decision"]["code"]

        with ThreadPoolExecutor(max_workers=8) as executor:
            codes = list(executor.map(evaluate, range(8)))
        self.assertEqual(codes.count("ALLOW"), 3)
        self.assertEqual(codes.count("REJECT_FREQUENCY"), 5)

    def test_unknown_execution_result_does_not_release_reservation(self):
        account_id = "stateful-unknown-execution"
        self.sync_account(account_id)
        request_id = "unknown-open-r1"
        self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            request_id=request_id,
        )
        _, body = self.request(
            "/v1/execution-result",
            {
                "account_id": account_id,
                "request_id": request_id,
                "outcome": "unknown",
                "action": "open",
                "symbol": "EURUSD",
                "occurred_at": datetime.now(UTC).isoformat(),
                "platform_status": "timeout",
            },
        )
        self.assertFalse(body["reservation_released"])
        self.assertEqual(
            len(self.server.state_store.frequency(account_id).open_times),
            1,
        )

    def test_failed_modify_releases_cooldown_reservation(self):
        account_id = "stateful-modify-release"
        self.sync_account(account_id)
        request_id = "modify-release-r1"
        now = datetime.now(UTC)
        _, decision = self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": {
                    "symbol": "EURUSD",
                    "action": "modify",
                    "requested_at": now.isoformat(),
                    "stop_loss": "1.0800",
                    "additional_risk": "100",
                    "is_risk_increasing": True,
                },
            },
            request_id=request_id,
        )
        self.assertEqual(decision["decision"]["code"], "ALLOW")
        self.assertIn(
            "EURUSD",
            self.server.state_store.frequency(
                account_id
            ).last_modify_by_symbol,
        )

        _, result = self.request(
            "/v1/execution-result",
            {
                "account_id": account_id,
                "request_id": request_id,
                "outcome": "failure",
                "action": "modify",
                "symbol": "EURUSD",
                "occurred_at": datetime.now(UTC).isoformat(),
                "platform_status": "rejected",
            },
        )
        self.assertTrue(result["reservation_released"])
        self.assertNotIn(
            "EURUSD",
            self.server.state_store.frequency(
                account_id
            ).last_modify_by_symbol,
        )

    def test_execution_retry_with_new_timestamp_replays_first_result(self):
        account_id = "stateful-execution-idempotent"
        self.sync_account(account_id)
        request_id = "execution-idempotent-r1"
        self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            request_id=request_id,
        )
        first_payload = {
            "account_id": account_id,
            "request_id": request_id,
            "outcome": "unknown",
            "action": "open",
            "symbol": "EURUSD",
            "occurred_at": datetime.now(UTC).isoformat(),
            "platform_status": "timeout",
        }
        _, first = self.request(
            "/v1/execution-result",
            first_payload,
        )
        second_payload = {
            **first_payload,
            "occurred_at": (
                datetime.now(UTC) + timedelta(seconds=1)
            ).isoformat(),
        }
        _, second = self.request(
            "/v1/execution-result",
            second_payload,
        )
        self.assertEqual(first, second)

    def test_execution_result_rejects_future_client_clock(self):
        account_id = "stateful-execution-clock"
        self.sync_account(account_id)
        request_id = "execution-clock-r1"
        self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            request_id=request_id,
        )
        with self.assertRaises(HTTPError) as context:
            self.request(
                "/v1/execution-result",
                {
                    "account_id": account_id,
                    "request_id": request_id,
                    "outcome": "success",
                    "action": "open",
                    "symbol": "EURUSD",
                    "occurred_at": (
                        datetime.now(UTC) + timedelta(minutes=2)
                    ).isoformat(),
                    "platform_status": "filled",
                },
            )
        self.assertEqual(context.exception.code, 400)

    def test_settlement_sync_clears_uncertain_day_state(self):
        account_id = "stateful-settlement"
        now = datetime.now(UTC)
        previous = now - timedelta(days=1)
        self.server.state_store.sync_account(
            account_id=account_id,
            account_type=AccountType.TWO_STEP,
            phase=AccountPhase.EVALUATION,
            style=AccountStyle.STANDARD,
            initial_capital=Decimal("100000"),
            balance=Decimal("100000"),
            equity=Decimal("100000"),
            current_open_risk=Decimal("0"),
            as_of=previous,
            received_at=previous,
            bootstrap_day_start_balance=Decimal("100000"),
            bootstrap_highest_settled_balance=Decimal("100000"),
        )
        self.request(
            "/v1/account-sync",
            {
                "account_id": account_id,
                "account_type": "two_step",
                "phase": "evaluation",
                "style": "standard",
                "initial_capital": "100000",
                "balance": "101000",
                "equity": "101000",
                "current_open_risk": "0",
                "as_of": now.isoformat(),
            },
        )
        account = self.server.state_store.get_account(account_id)
        self.assertTrue(account.snapshot.data_uncertain)
        settlement_day = ftmo_day_key(now)
        self.request(
            "/v1/settlement-sync",
            {
                "account_id": account_id,
                "ftmo_day": settlement_day,
                "settled_balance": "100000",
                "settled_at": now.isoformat(),
                "source": "test-settlement",
            },
        )
        account = self.server.state_store.get_account(account_id)
        self.assertFalse(account.snapshot.data_uncertain)

    def test_news_status_force_flats_before_hard_window(self):
        account_id = "stateful-news"
        self.sync_account(account_id, phase="ftmo_account")
        now = datetime.now(UTC)
        self.request(
            "/v1/news-sync",
            {
                "fetched_at": now.isoformat(),
                "events": [
                    {
                        "event_id": "NFP",
                        "release_time": (
                            now + timedelta(minutes=5)
                        ).isoformat(),
                        "affected_symbols": ["EURUSD"],
                    }
                ],
            },
        )
        _, body = self.request(
            "/v1/news-status",
            {
                "account_id": account_id,
                "symbol": "EURUSD",
                "now": now.isoformat(),
            },
        )
        self.assertTrue(body["force_flat"])
        self.assertTrue(body["cancel_pending"])
        self.assertFalse(body["hard_window"])

    def test_news_status_does_not_force_close_inside_hard_window(self):
        account_id = "stateful-hard-news"
        self.sync_account(account_id, phase="ftmo_account")
        now = datetime.now(UTC)
        self.request(
            "/v1/news-sync",
            {
                "fetched_at": now.isoformat(),
                "events": [
                    {
                        "event_id": "CPI",
                        "release_time": (
                            now + timedelta(minutes=1)
                        ).isoformat(),
                        "affected_symbols": ["EURUSD"],
                    }
                ],
            },
        )
        _, body = self.request(
            "/v1/news-status",
            {
                "account_id": account_id,
                "symbol": "EURUSD",
                "now": now.isoformat(),
            },
        )
        self.assertTrue(body["hard_window"])
        self.assertTrue(body["emergency_alert"])
        self.assertFalse(body["force_flat"])

    def test_status_endpoints_reject_stale_client_clock(self):
        account_id = "stateful-status-clock"
        self.sync_account(account_id, phase="ftmo_account")
        stale_now = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        for path in ("/v1/news-status", "/v1/market-status"):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as context:
                    self.request(
                        path,
                        {
                            "account_id": account_id,
                            "symbol": "EURUSD",
                            "now": stale_now,
                        },
                    )
                self.assertEqual(context.exception.code, 400)

    def test_market_status_force_flats_before_long_close(self):
        account_id = "stateful-market-close"
        self.sync_account(account_id, phase="ftmo_account")
        now = datetime.now(UTC)
        self.request(
            "/v1/market-sync",
            {
                "fetched_at": now.isoformat(),
                "closures": [
                    {
                        "closure_id": "weekend",
                        "start_time": (
                            now + timedelta(minutes=5)
                        ).isoformat(),
                        "end_time": (
                            now + timedelta(days=2)
                        ).isoformat(),
                        "affected_symbols": ["EURUSD"],
                    }
                ],
            },
        )
        _, body = self.request(
            "/v1/market-status",
            {
                "account_id": account_id,
                "symbol": "EURUSD",
                "now": now.isoformat(),
            },
        )
        self.assertTrue(body["force_flat"])
        self.assertTrue(body["cancel_pending"])
        self.assertFalse(body["closure_active"])

    def test_stateful_evaluation_rejects_market_close_window(self):
        account_id = "stateful-market-evaluate"
        self.sync_account(account_id, phase="ftmo_account")
        now = datetime.now(UTC)
        self.request(
            "/v1/news-sync",
            {"fetched_at": now.isoformat(), "events": []},
        )
        self.request(
            "/v1/market-sync",
            {
                "fetched_at": now.isoformat(),
                "closures": [
                    {
                        "closure_id": "weekend",
                        "start_time": (
                            now + timedelta(minutes=5)
                        ).isoformat(),
                        "end_time": (
                            now + timedelta(days=2)
                        ).isoformat(),
                        "affected_symbols": ["EURUSD"],
                    }
                ],
            },
        )
        _, body = self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(now.isoformat()),
            },
            request_id="market-evaluate-r1",
        )
        self.assertEqual(
            body["decision"]["code"],
            "REJECT_MARKET_CLOSE",
        )

    def test_stateful_evaluation_uses_server_time_for_news_window(self):
        account_id = "stateful-server-time"
        self.sync_account(account_id, phase="ftmo_account")
        now = datetime.now(UTC)
        self.request(
            "/v1/news-sync",
            {
                "fetched_at": now.isoformat(),
                "events": [
                    {
                        "event_id": "SERVER-TIME",
                        "release_time": (
                            now + timedelta(minutes=2, seconds=15)
                        ).isoformat(),
                        "affected_symbols": ["EURUSD"],
                    }
                ],
            },
        )
        _, body = self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(
                    (now + timedelta(seconds=29)).isoformat()
                ),
            },
            request_id="server-time-r1",
        )
        self.assertEqual(body["decision"]["code"], "REJECT_NEWS")
        self.assertIn(
            "internal news buffer",
            body["decision"]["reasons"][0],
        )


if __name__ == "__main__":
    unittest.main()
