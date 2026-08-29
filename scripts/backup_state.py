"""Create an owner-only, consistency-checked SQLite state backup."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from src.state_store import StateStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state",
        default=os.environ.get("RISK_STATE_PATH", "runtime/risk-state.db"),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    state_path = Path(args.state).expanduser()
    if not state_path.is_file():
        parser.error(f"state database does not exist: {state_path}")
    store = StateStore(state_path)
    try:
        destination = store.backup_to(Path(args.output))
    finally:
        store.close()
    print(
        json.dumps(
            {
                "ok": True,
                "operation": "backup",
                "state": str(Path(args.state).resolve()),
                "output": str(destination),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
