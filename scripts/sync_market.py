"""Push a reviewed long market-closure schedule into the local risk API."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file",
        default="config/market-closures.example.json",
        help="Reviewed symbol closure JSON file",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--token",
        default=os.environ.get("RISK_API_TOKEN", ""),
    )
    args = parser.parse_args()

    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    payload["fetched_at"] = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(payload).encode("utf-8")
    request = Request(
        args.url.rstrip("/") + "/v1/market-sync",
        data=encoded,
        headers={
            "Content-Type": "application/json",
            "X-Risk-Token": args.token,
        },
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        print(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
