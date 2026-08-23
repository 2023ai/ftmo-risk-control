"""Push reviewed qualification history into one account-scoped risk API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen
from uuid import uuid4


def _post(
    *,
    url: str,
    path: str,
    credential: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    request = Request(
        url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Account-Credential": credential,
            "X-Request-Id": uuid4().hex,
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        body = json.loads(response.read())
    if not isinstance(body, dict):
        raise ValueError("risk API response must be a JSON object")
    return body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file",
        default="config/qualification-history.example.json",
        help="Reviewed qualification history JSON file",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--credential",
        default=os.environ.get("RISK_ACCOUNT_CREDENTIAL", ""),
    )
    args = parser.parse_args()
    if not args.credential:
        parser.error(
            "--credential or RISK_ACCOUNT_CREDENTIAL is required"
        )

    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        parser.error("qualification history file must be a JSON object")
    shared = {
        "account_id": raw["account_id"],
        "phase": raw["phase"],
        "cycle_id": raw["cycle_id"],
        "source": raw.get("source", "reviewed-platform-export"),
    }
    results: dict[str, Any] = {
        "closed_trades": [],
        "trading_days": [],
    }
    for trade in raw.get("closed_trades", []):
        results["closed_trades"].append(
            _post(
                url=args.url,
                path="/v1/closed-trade-sync",
                credential=args.credential,
                payload={**shared, **trade},
            )
        )
    for opened in raw.get("opened_positions", []):
        results["trading_days"].append(
            _post(
                url=args.url,
                path="/v1/trading-day-sync",
                credential=args.credential,
                payload={
                    **shared,
                    "opened_at": opened["opened_at"],
                    "request_id": opened.get(
                        "event_id",
                        uuid4().hex,
                    ),
                },
            )
        )
    results["history"] = _post(
        url=args.url,
        path="/v1/qualification-history-sync",
        credential=args.credential,
        payload={
            **shared,
            "history_start_at": raw["history_start_at"],
            "complete_through": raw["complete_through"],
        },
    )
    print(json.dumps(results, ensure_ascii=True))


if __name__ == "__main__":
    main()
