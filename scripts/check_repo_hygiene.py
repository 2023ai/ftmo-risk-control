"""Reject runtime artifacts and high-confidence secrets in tracked files."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


FORBIDDEN_SUFFIXES = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".db-wal",
        ".db-shm",
        ".log",
        ".ex4",
        ".ex5",
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".crt",
        ".cer",
    }
)
FORBIDDEN_DIRECTORIES = frozenset({"runtime", "backups", "secrets"})
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [
        Path(item)
        for item in result.stdout.decode("utf-8").split("\0")
        if item
    ]


def main() -> int:
    errors: list[str] = []
    try:
        files = _tracked_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"repository hygiene check could not list tracked files: {exc}")
        return 2

    for path in files:
        lowered_name = path.name.lower()
        if (
            any(part.lower() in FORBIDDEN_DIRECTORIES for part in path.parts)
            or any(
                lowered_name.endswith(suffix)
                for suffix in FORBIDDEN_SUFFIXES
            )
        ):
            errors.append(f"tracked runtime or credential artifact: {path}")
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            errors.append(f"could not read tracked file {path}: {exc}")
            continue
        if b"\0" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(
                    f"high-confidence secret pattern found in tracked file: {path}"
                )
                break

    if errors:
        print("Repository hygiene check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Repository hygiene check passed for {len(files)} tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
