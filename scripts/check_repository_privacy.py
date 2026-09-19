#!/usr/bin/env python3
"""Fail when Git tracks common private runtime artifacts."""

from __future__ import annotations

import subprocess
from pathlib import PurePosixPath


FORBIDDEN_FILENAMES = {".env", "app.env"}
FORBIDDEN_ENDINGS = {
    ".7z",
    ".bak",
    ".backup",
    ".csv",
    ".db",
    ".db-shm",
    ".db-wal",
    ".dump",
    ".gz",
    ".jpeg",
    ".jpg",
    ".key",
    ".log",
    ".p12",
    ".pdf",
    ".pem",
    ".pfx",
    ".png",
    ".sql",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".tar",
    ".tar.gz",
    ".tsv",
    ".webp",
    ".xls",
    ".xlsx",
    ".zip",
}
FORBIDDEN_PARTS = {
    "backups",
    "data",
    "exports",
    "imports",
    "logs",
    "movimientos",
    "screenshots",
    "uploads",
}
ALLOWED_FILES = {"favicon.png"}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    violations: list[str] = []
    for filename in tracked_files():
        path = PurePosixPath(filename)
        if filename in ALLOWED_FILES:
            continue
        lower_name = path.name.lower()
        if lower_name in FORBIDDEN_FILENAMES or (lower_name.startswith(".env.") and lower_name != ".env.example"):
            violations.append(filename)
            continue
        if any(lower_name.endswith(ending) for ending in FORBIDDEN_ENDINGS):
            violations.append(filename)
            continue
        if any(part.lower() in FORBIDDEN_PARTS for part in path.parts[:-1]):
            violations.append(filename)

    if violations:
        print("Sensitive artifacts are tracked:")
        for filename in sorted(violations):
            print(f"- {filename}")
        return 1

    print(f"Privacy path check passed ({len(tracked_files())} tracked files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
