#!/usr/bin/env python3
"""Run real PostgreSQL, S3-compatible, and Temporal round trips."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import close_database, database_round_trip
from app.object_store import object_store_round_trip
from app.temporal_client import temporal_round_trip


async def main() -> None:
    settings.assert_durable_control_plane_config_safe()
    try:
        evidence = {
            "postgresql": await database_round_trip(),
            "object_store": await object_store_round_trip(),
            "temporal": await temporal_round_trip(),
        }
        print(
            json.dumps(
                {
                    "status": "success",
                    "dependencies": evidence,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
