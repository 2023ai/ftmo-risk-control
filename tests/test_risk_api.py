import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http import HTTPStatus

from src.risk_api import RequestError, evaluate_payload, make_server
from src.risk_engine import (
    AccountPhase,
    AccountStyle,
    AccountType,
    ftmo_day_key,
)
from src.state_store import StateStore


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
        "open_positions_count": 0,
        "pending_orders_count": 0,
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


def _calendar_coverage(
    reference: datetime | None = None,
) -> dict[str, str]:
    reference = reference or datetime.now(UTC)
    return {
        "coverage_start": (reference - timedelta(days=1)).isoformat(),
        "coverage_end": (reference + timedelta(days=7)).isoformat(),
    }


def _with_calendar_coverage(
    path: str,
    payload: dict | None,
) -> dict | None:
    if payload is None or path not in {"/v1/news-sync", "/v1/market-sync"}:
        return payload
    if "coverage_start" in payload or "coverage_end" in payload:
        return payload
    return {**payload, **_calendar_coverage()}


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
        payload = _with_calendar_coverage(path, payload)
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
        self.assertEqual(body["rule_version"], "ftmo-v5-2026-08-26")
        self.assertFalse(body["database_up"])
        self.assertFalse(body["ready_for_risk_increase"])
        self.assertIn(
            "persistent state database is unavailable",
            body["readiness_reasons"],
        )
        self.assertIsNone(body["unknown_execution_records"])

    def test_ready_returns_service_unavailable_without_persistent_state(self):
        with self.assertRaises(HTTPError) as context:
            self.request("GET", "/ready")
        self.assertEqual(context.exception.code, 503)
        body = json.loads(context.exception.read())
        self.assertFalse(body["ok"])
        self.assertFalse(body["ready_for_risk_increase"])
        context.exception.close()

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
        event_time = datetime.now(UTC)
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
        event_time = datetime.now(UTC) + timedelta(minutes=30)
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

    def test_calendar_sync_requires_explicit_coverage(self):
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-sync",
                {
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "coverage_start": None,
                    "coverage_end": None,
                    "events": [],
                },
            )
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

    def test_fresh_but_undercovered_calendar_is_fail_closed(self):
        now = datetime.now(UTC)
        self.request(
            "POST",
            "/v1/news-sync",
            {
                "fetched_at": now.isoformat(),
                "coverage_start": (now - timedelta(minutes=1)).isoformat(),
                "coverage_end": (now + timedelta(minutes=1)).isoformat(),
                "events": [],
            },
        )
        status, body = self.request(
            "POST",
            "/v1/evaluate",
            {
                "account_type": "two_step",
                "phase": "evaluation",
                "style": "standard",
                "snapshot": _snapshot(),
                "request": _open_request(),
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["code"], "REJECT_DATA_STALE")
        self.assertFalse(self.server.calendar_health("news")["coverage_sufficient"])

    def test_calendar_entries_must_match_declared_coverage(self):
        now = datetime.now(UTC)
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-sync",
                {
                    "fetched_at": now.isoformat(),
                    "coverage_start": (now - timedelta(minutes=5)).isoformat(),
                    "coverage_end": (now + timedelta(minutes=5)).isoformat(),
                    "events": [
                        {
                            "event_id": "OUTSIDE-COVERAGE",
                            "release_time": (
                                now + timedelta(hours=1)
                            ).isoformat(),
                            "affected_symbols": ["EURUSD"],
                        }
                    ],
                },
            )
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

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

    def test_position_size_reserves_estimated_costs(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "loss_per_volume_unit": "100",
            "estimated_costs": "50",
            "volume_step": "0.01",
            "min_volume": "0.01",
        }
        status, body = self.request("POST", "/v1/position-size", payload)
        self.assertEqual(status, 200)
        self.assertEqual(
            Decimal(body["position_size"]["volume"]),
            Decimal("2.00"),
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

    def test_invalid_calendar_age_is_bad_request(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": _open_request(),
            "news_data_age_seconds": "0",
            "market_data_age_seconds": 0,
        }
        with self.assertRaises(RequestError):
            evaluate_payload(payload, "config/ftmo-v2.json")

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

    def test_negative_trade_volume_is_bad_request(self):
        payload = {
            "account_type": "two_step",
            "phase": "evaluation",
            "style": "standard",
            "snapshot": _snapshot(),
            "request": {
                **_open_request(),
                "volume": "-1",
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
            coverage_start = self.server.news_coverage_start
            coverage_end = self.server.news_coverage_end
        self.assertIsNotNone(fetched_at)
        self.assertIsNotNone(coverage_start)
        self.assertIsNotNone(coverage_end)
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-sync",
                {
                    "fetched_at": fetched_at.isoformat(),
                    "coverage_start": coverage_start.isoformat(),
                    "coverage_end": coverage_end.isoformat(),
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

    def test_equal_calendar_timestamp_cannot_change_coverage(self):
        with self.server.news_lock:
            fetched_at = self.server.news_fetched_at
            coverage_start = self.server.news_coverage_start
            coverage_end = self.server.news_coverage_end
        self.assertIsNotNone(fetched_at)
        self.assertIsNotNone(coverage_start)
        self.assertIsNotNone(coverage_end)
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-sync",
                {
                    "fetched_at": fetched_at.isoformat(),
                    "coverage_start": coverage_start.isoformat(),
                    "coverage_end": (
                        coverage_end + timedelta(days=1)
                    ).isoformat(),
                    "events": [],
                },
            )
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

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

    def test_calendar_symbol_wildcard_must_be_trailing(self):
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-sync",
                {
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "events": [
                        {
                            "event_id": "BAD-PATTERN",
                            "release_time": datetime.now(UTC).isoformat(),
                            "affected_symbols": ["EUR*USD"],
                        }
                    ],
                },
            )
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

    def test_duplicate_json_keys_are_rejected(self):
        request = Request(
            self.base_url + "/v1/news-sync",
            data=(
                b'{"fetched_at":"2026-08-22T12:00:00+00:00",'
                b'"events":[],"events":[]}'
            ),
            headers={
                "Content-Type": "application/json",
                "X-Risk-Token": "test-token",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as context:
            urlopen(request, timeout=2)
        self.assertEqual(context.exception.code, 400)
        context.exception.close()


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

    def test_mtls_requires_server_certificate_and_key(self):
        with self.assertRaises(ValueError):
            make_server(
                "127.0.0.1",
                0,
                "config/ftmo-v2.json",
                auth_token="test-token",
                tls_ca_path="test-ca.pem",
                require_client_cert=True,
            )

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
                    "ftmo-v5-2026-08-26",
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

    def test_existing_state_lock_is_checked_before_opening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "risk.db")
            server = make_server(
                "127.0.0.1",
                0,
                "config/ftmo-v2.json",
                auth_token="test-token",
                state_path=state_path,
            )
            try:
                with patch(
                    "src.risk_api.StateStore",
                    side_effect=AssertionError("state store must not open"),
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "already owned by server process",
                    ):
                        make_server(
                            "127.0.0.1",
                            0,
                            "config/ftmo-v2.json",
                            auth_token="test-token",
                            state_path=state_path,
                        )
            finally:
                server.server_close()

    def test_unsafe_state_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "risk.db")
            os.mkdir(state_path + ".server.lock")
            with self.assertRaisesRegex(
                ValueError,
                "lock must be a regular file",
            ):
                make_server(
                    "127.0.0.1",
                    0,
                    "config/ftmo-v2.json",
                    auth_token="test-token",
                    state_path=state_path,
                )


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
            require_account_credentials=False,
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
        payload = _with_calendar_coverage(path, payload)
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
                "open_positions_count": 0,
                "pending_orders_count": 0,
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

    def test_unknown_execution_is_a_server_side_risk_lock_and_can_resolve(self):
        account_id = "stateful-server-unknown-lock"
        self.sync_account(account_id)
        first_request_id = "server-unknown-open-r1"
        self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            request_id=first_request_id,
        )
        self.request(
            "/v1/execution-result",
            {
                "account_id": account_id,
                "request_id": first_request_id,
                "outcome": "unknown",
                "action": "open",
                "symbol": "EURUSD",
                "occurred_at": datetime.now(UTC).isoformat(),
                "platform_status": "timeout",
            },
        )

        _, blocked = self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            request_id="server-unknown-open-r2",
        )
        self.assertEqual(
            blocked["decision"]["code"],
            "REJECT_UNKNOWN_EXECUTION",
        )

        _, reducing = self.request(
            "/v1/evaluate",
            {
                "account_id": account_id,
                "request": {
                    "symbol": "EURUSD",
                    "action": "close",
                    "requested_at": datetime.now(UTC).isoformat(),
                    "is_risk_increasing": False,
                },
            },
            request_id="server-unknown-close-r1",
        )
        self.assertEqual(reducing["decision"]["code"], "ALLOW")

        _, resolved = self.request(
            "/v1/execution-result",
            {
                "account_id": account_id,
                "request_id": first_request_id,
                "outcome": "failure",
                "action": "open",
                "symbol": "EURUSD",
                "occurred_at": datetime.now(UTC).isoformat(),
                "platform_status": "rejected",
            },
        )
        self.assertTrue(resolved["resolved_unknown"])
        self.assertTrue(resolved["reservation_released"])
        self.assertEqual(
            self.server.state_store.unknown_execution_count(account_id),
            0,
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
        self.assertTrue(body["cancel_pending"])

    def test_stale_news_status_cancels_pending_orders(self):
        account_id = "stateful-stale-news"
        self.sync_account(account_id, phase="ftmo_account")
        with self.server.news_lock:
            self.server.news_fetched_at = datetime.now(UTC) - timedelta(
                seconds=61
            )
        _, body = self.request(
            "/v1/news-status",
            {
                "account_id": account_id,
                "symbol": "EURUSD",
                "now": datetime.now(UTC).isoformat(),
            },
        )
        self.assertTrue(body["news_data_stale"])
        self.assertTrue(body["cancel_pending"])

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

    def test_active_market_closure_cancels_pending_orders(self):
        account_id = "stateful-active-market-close"
        self.sync_account(account_id, phase="evaluation")
        now = datetime.now(UTC)
        self.request(
            "/v1/market-sync",
            {
                "fetched_at": now.isoformat(),
                "closures": [
                    {
                        "closure_id": "active-break",
                        "start_time": (
                            now - timedelta(minutes=1)
                        ).isoformat(),
                        "end_time": (
                            now + timedelta(hours=3)
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
        self.assertTrue(body["closure_active"])
        self.assertTrue(body["cancel_pending"])

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


class CredentialQualificationAPITests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = f"{self.tempdir.name}/risk.db"
        self.server = make_server(
            "127.0.0.1",
            0,
            "config/ftmo-v2.json",
            auth_token="admin-token",
            state_path=self.state_path,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tempdir.cleanup()

    def request(
        self,
        method,
        path,
        payload=None,
        *,
        admin=False,
        credential=None,
        bearer=False,
    ):
        headers = {}
        data = None
        payload = _with_calendar_coverage(path, payload)
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        if admin:
            if bearer:
                headers["Authorization"] = "Bearer admin-token"
            else:
                headers["X-Risk-Token"] = "admin-token"
        if credential is not None:
            headers["X-Account-Credential"] = credential
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        with urlopen(request, timeout=2) as response:
            content_type = response.headers.get("Content-Type", "")
            content = response.read()
            if content_type.startswith("application/json"):
                return response.status, json.loads(content)
            return response.status, content.decode("utf-8")

    def bootstrap_account(
        self,
        account_id="credential-account",
        *,
        account_type="two_step",
        phase="evaluation",
        balance="100000",
        open_positions_count=0,
        pending_orders_count=0,
        scopes=None,
    ):
        now = datetime.now(UTC).isoformat()
        self.request(
            "POST",
            "/v1/account-sync",
            {
                "account_id": account_id,
                "account_type": account_type,
                "phase": phase,
                "style": "standard",
                "initial_capital": "100000",
                "day_start_balance": "100000",
                "highest_settled_balance": "100000",
                "balance": balance,
                "equity": "100000",
                "current_open_risk": "0",
                "open_positions_count": open_positions_count,
                "pending_orders_count": pending_orders_count,
                "as_of": now,
            },
            admin=True,
        )
        _, created = self.request(
            "POST",
            f"/v1/admin/accounts/{account_id}/credentials",
            {"scopes": scopes} if scopes is not None else {},
            admin=True,
        )
        credential = created["secret"]
        for path, values in (
            ("/v1/news-sync", {"events": []}),
            ("/v1/market-sync", {"closures": []}),
        ):
            self.request(
                "POST",
                path,
                {"fetched_at": datetime.now(UTC).isoformat(), **values},
                admin=True,
            )
        return credential, created["credential"]["credential_id"]

    def test_ready_succeeds_with_persistent_state_and_covered_calendars(self):
        self.bootstrap_account("ready-account")
        status, body = self.request("GET", "/ready", admin=True)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["ready_for_risk_increase"])
        self.assertEqual(body["readiness_reasons"], [])

    def test_credential_list_rejects_invalid_or_repeated_account_query(self):
        for query in (
            "account_id=invalid%2Faccount",
            "account_id=first&account_id=second",
        ):
            with self.subTest(query=query):
                with self.assertRaises(HTTPError) as context:
                    self.request(
                        "GET",
                        f"/v1/admin/credentials?{query}",
                        admin=True,
                    )
                self.assertEqual(context.exception.code, 400)
                context.exception.close()

    def test_account_credential_is_bound_to_account_and_scope(self):
        credential, _ = self.bootstrap_account("bound-account")
        status, body = self.request(
            "POST",
            "/v1/evaluate",
            {
                "account_id": "bound-account",
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            credential=credential,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["decision"]["code"], "ALLOW")

        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/evaluate",
                {
                    "account_id": "different-account",
                    "request": _open_request(datetime.now(UTC).isoformat()),
                },
                credential=credential,
            )
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/evaluate",
                {
                    "account_id": "bound-account",
                    "request": _open_request(datetime.now(UTC).isoformat()),
                },
                admin=True,
            )
        self.assertEqual(context.exception.code, 403)
        context.exception.close()

    def test_unprovisioned_account_cannot_receive_platform_credential(self):
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/admin/accounts/unprovisioned/credentials",
                {},
                admin=True,
            )
        self.assertEqual(context.exception.code, 400)
        context.exception.close()

    def test_orphaned_credential_cannot_bootstrap_an_account(self):
        now = datetime.now(UTC)
        _, credential = self.server.state_store.create_account_credential(
            account_id="orphaned-account",
            scopes=("account:sync",),
            not_before=now,
            expires_at=now + timedelta(hours=1),
            now=now,
        )
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/account-sync",
                {
                    "account_id": "orphaned-account",
                    "account_type": "two_step",
                    "phase": "evaluation",
                    "style": "standard",
                    "initial_capital": "100000",
                    "day_start_balance": "100000",
                    "highest_settled_balance": "100000",
                    "balance": "100000",
                    "equity": "100000",
                    "current_open_risk": "0",
                    "open_positions_count": 0,
                    "pending_orders_count": 0,
                    "as_of": now.isoformat(),
                },
                credential=credential,
            )
        self.assertEqual(context.exception.code, 403)
        context.exception.close()

    def test_default_platform_credential_has_no_settlement_scope(self):
        credential, _ = self.bootstrap_account("least-privilege")
        now = datetime.now(UTC)
        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/settlement-sync",
                {
                    "account_id": "least-privilege",
                    "ftmo_day": ftmo_day_key(now),
                    "settled_balance": "100000",
                    "settled_at": now.isoformat(),
                    "source": "unauthorized-platform",
                },
                credential=credential,
            )
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

    def test_account_credential_can_rotate_without_secret_disclosure_later(self):
        old_secret, credential_id = self.bootstrap_account("rotate-api")
        _, rotated = self.request(
            "POST",
            f"/v1/admin/credentials/{credential_id}/rotate",
            {"overlap_seconds": 0},
            admin=True,
        )
        new_secret = rotated["secret"]
        self.assertNotEqual(old_secret, new_secret)

        with self.assertRaises(HTTPError) as context:
            self.request(
                "POST",
                "/v1/news-status",
                {
                    "account_id": "rotate-api",
                    "symbol": "EURUSD",
                    "now": datetime.now(UTC).isoformat(),
                },
                credential=old_secret,
            )
        self.assertEqual(context.exception.code, 401)
        context.exception.close()

        status, body = self.request(
            "POST",
            "/v1/news-status",
            {
                "account_id": "rotate-api",
                "symbol": "EURUSD",
                "now": datetime.now(UTC).isoformat(),
            },
            credential=new_secret,
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["news_data_stale"])

        _, listed = self.request(
            "GET",
            "/v1/admin/credentials?account_id=rotate-api",
            admin=True,
        )
        self.assertNotIn("secret", listed["credentials"][0])

    def test_settlement_endpoint_accepts_account_settlement_scope(self):
        now = datetime.now(UTC)
        self.request(
            "POST",
            "/v1/account-sync",
            {
                "account_id": "settlement-scope",
                "account_type": "two_step",
                "phase": "evaluation",
                "style": "standard",
                "initial_capital": "100000",
                "day_start_balance": "100000",
                "highest_settled_balance": "100000",
                "balance": "100000",
                "equity": "100000",
                "current_open_risk": "0",
                "open_positions_count": 0,
                "as_of": now.isoformat(),
            },
            admin=True,
        )
        _, created = self.request(
            "POST",
            "/v1/admin/accounts/settlement-scope/credentials",
            {"scopes": ["account:settlement"]},
            admin=True,
        )
        credential = created["secret"]
        status, body = self.request(
            "POST",
            "/v1/settlement-sync",
            {
                "account_id": "settlement-scope",
                "ftmo_day": ftmo_day_key(now),
                "settled_balance": "100000",
                "settled_at": now.isoformat(),
                "source": "test-settlement",
            },
            credential=credential,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["ftmo_day"], ftmo_day_key(now))

    def test_calendar_from_old_rule_version_is_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "risk.db")
            now = datetime.now(UTC)
            StateStore(state_path).save_calendar_snapshot(
                calendar_type="news",
                fetched_at=now,
                payload=[],
                rule_version="old-rule",
            )
            config_path = os.path.join(directory, "config.json")
            with open("config/ftmo-v2.json", encoding="utf-8") as source:
                config = json.load(source)
            config["rule_version"] = "new-rule"
            with open(config_path, "w", encoding="utf-8") as target:
                json.dump(config, target)
            server = make_server(
                "127.0.0.1",
                0,
                config_path,
                auth_token="admin-token",
                state_path=state_path,
            )
            try:
                self.assertEqual(server.news_events, [])
                health = server.calendar_health("news")
                self.assertFalse(health["present"])
                self.assertFalse(health["rule_version_match"])
                self.assertIn(
                    'calendar="news",result="rule_mismatch"',
                    server.metrics_text(),
                )
            finally:
                server.server_close()

    def test_prometheus_endpoint_labels_are_bounded(self):
        self.server.observe_response(
            "GET",
            '/bad"quote',
            HTTPStatus.OK,
            {},
        )
        self.server.observe_response(
            "GET",
            "/another-random-path",
            HTTPStatus.OK,
            {},
        )
        self.server.observe_response(
            "POST",
            "/v1/admin/credentials/first/rotate",
            HTTPStatus.OK,
            {},
        )
        self.server.observe_response(
            "POST",
            "/v1/admin/credentials/second/rotate",
            HTTPStatus.OK,
            {},
        )
        metrics = self.server.metrics_text()
        self.assertIn('endpoint="/__unknown__",status="200"} 2', metrics)
        self.assertIn(
            'endpoint="/v1/admin/credentials/{credential_id}/rotate",'
            'status="200"} 2',
            metrics,
        )
        self.assertNotIn("another-random-path", metrics)

    def test_qualification_dashboard_uses_closed_trade_history(self):
        credential, _ = self.bootstrap_account(
            "qualification-api",
            balance="110000",
            open_positions_count=0,
            scopes=["qualification:read", "qualification:write"],
        )
        for index in range(4):
            closed_at = datetime(
                2026,
                8,
                19 + index,
                12,
                0,
                tzinfo=UTC,
            )
            self.request(
                "POST",
                "/v1/closed-trade-sync",
                {
                    "account_id": "qualification-api",
                    "trade_id": f"closed-{index}",
                    "phase": "evaluation",
                    "cycle_id": "challenge-1",
                    "closed_at": closed_at.isoformat(),
                    "net_profit": "2500",
                    "symbol": "EURUSD",
                    "source": "test-history",
                },
                credential=credential,
            )
            self.request(
                "POST",
                "/v1/trading-day-sync",
                {
                    "account_id": "qualification-api",
                    "phase": "evaluation",
                    "cycle_id": "challenge-1",
                    "opened_at": closed_at.isoformat(),
                    "source": "test-history",
                },
                credential=credential,
            )
        self.request(
            "POST",
            "/v1/qualification-history-sync",
            {
                "account_id": "qualification-api",
                "phase": "evaluation",
                "cycle_id": "challenge-1",
                "history_start_at": "2026-08-01T00:00:00+00:00",
                "complete_through": datetime.now(UTC).isoformat(),
                "source": "test-history",
            },
            credential=credential,
        )
        status, body = self.request(
            "GET",
            "/v1/qualification?account_id=qualification-api",
            credential=credential,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["profit_target"]["met"])
        self.assertTrue(body["minimum_trading_days"]["met"])
        self.assertTrue(body["eligible"])

        _, dashboard = self.request(
            "GET",
            "/v1/qualification/accounts",
            admin=True,
        )
        self.assertEqual(dashboard["accounts"][0]["account_id"], "qualification-api")

    def test_calendar_persistence_and_prometheus_metrics(self):
        credential, _ = self.bootstrap_account("calendar-restore")
        event_time = datetime.now(UTC) + timedelta(minutes=30)
        self.request(
            "POST",
            "/v1/news-sync",
            {
                "fetched_at": datetime.now(UTC).isoformat(),
                "events": [
                    {
                        "event_id": "PERSISTED",
                        "release_time": event_time.isoformat(),
                        "affected_symbols": ["EURUSD"],
                    }
                ],
            },
            admin=True,
        )
        self.request(
            "POST",
            "/v1/evaluate",
            {
                "account_id": "calendar-restore",
                "request": _open_request(datetime.now(UTC).isoformat()),
            },
            credential=credential,
        )
        _, metrics = self.request(
            "GET",
            "/metrics",
            admin=True,
            bearer=True,
        )
        self.assertIn("ftmo_risk_database_up 1", metrics)
        self.assertIn(
            'ftmo_risk_calendar_present{calendar="news"} 1',
            metrics,
        )
        self.assertIn(
            'ftmo_risk_calendar_stale{calendar="news"} 0',
            metrics,
        )
        self.assertIn(
            'ftmo_risk_calendar_coverage_sufficient{calendar="news"} 1',
            metrics,
        )
        self.assertIn("ftmo_risk_ready_for_risk_increase 1", metrics)
        self.assertIn(
            'ftmo_risk_decisions_total{code="ALLOW"} 1',
            metrics,
        )

        self.request(
            "POST",
            "/v1/account-sync",
            {
                "account_id": "metrics-account",
                "account_type": "two_step",
                "phase": "evaluation",
                "style": "standard",
                "initial_capital": "100000",
                "day_start_balance": "100000",
                "highest_settled_balance": "100000",
                "balance": "100000",
                "equity": "100000",
                "current_open_risk": "0",
                "open_positions_count": 0,
                "pending_orders_count": 0,
                "as_of": datetime.now(UTC).isoformat(),
            },
            admin=True,
        )
        _, credential = self.request(
            "POST",
            "/v1/admin/accounts/metrics-account/credentials",
            {
                "expires_at": (
                    datetime.now(UTC) + timedelta(hours=1)
                ).isoformat()
            },
            admin=True,
        )
        self.assertTrue(credential["secret"])
        _, metrics = self.request(
            "GET",
            "/metrics",
            admin=True,
            bearer=True,
        )
        self.assertIn("ftmo_risk_credentials_expiring_soon 1", metrics)

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = make_server(
            "127.0.0.1",
            0,
            "config/ftmo-v2.json",
            auth_token="admin-token",
            state_path=self.state_path,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.assertEqual(self.server.news_events[0].event_id, "PERSISTED")

    def test_qualification_dashboard_html_is_available_without_data(self):
        status, body = self.request(
            "GET",
            "/dashboard/qualification",
        )
        self.assertEqual(status, 200)
        self.assertIn("FTMO 资格看板", body)
        self.assertIn("/v1/qualification/accounts", body)

    def test_metrics_reports_database_down_without_failing_scrape(self):
        os.unlink(self.state_path)
        status, body = self.request(
            "GET",
            "/metrics",
            admin=True,
            bearer=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("ftmo_risk_database_up 0", body)


if __name__ == "__main__":
    unittest.main()
