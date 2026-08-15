#!/usr/bin/env python3
"""Import, inspect, or roll back legacy product stores."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import close_database
from app.migrations.legacy_import import (
    STORE_INVENTORY,
    LegacySourcePaths,
    capture_legacy_snapshot,
    import_legacy_data,
    rollback_legacy_import,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate SQLite/filesystem product data into PostgreSQL/S3",
    )
    parser.add_argument(
        "action",
        choices=("inventory", "import", "rollback"),
    )
    parser.add_argument(
        "--source-fingerprint",
        help="required for rollback",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict:
    paths = LegacySourcePaths.from_settings()
    if args.action == "inventory":
        snapshot = capture_legacy_snapshot(paths)
        return {
            "status": "success",
            "source_fingerprint": snapshot.source_fingerprint,
            "source_counts": snapshot.source_counts,
            "stores": STORE_INVENTORY,
        }
    if args.action == "import":
        return (await import_legacy_data(paths)).as_dict()
    if not args.source_fingerprint:
        raise SystemExit("--source-fingerprint is required for rollback")
    return await rollback_legacy_import(args.source_fingerprint)


async def main() -> None:
    args = _arguments()
    try:
        print(
            json.dumps(
                await _run(args),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
