"""Reconcile current local identities into the durable control plane.

The bridge is intentionally narrow for M1 Task 2: it copies public user
identity fields and fingerprints configured legacy API keys. Password hashes,
session tokens, API-key bytes, verification codes, and invite codes never enter
PostgreSQL. The complete legacy-store cutover remains a later migration task.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings
from app.repositories.identity import (  # noqa: E402
    reconcile_api_key,
    reconcile_authenticated_user,
    reconcile_local_anonymous,
)
from app.storage import auth as legacy_auth  # noqa: E402


async def reconcile() -> dict[str, int]:
    settings.assert_durable_control_plane_config_safe()
    legacy_db = await legacy_auth.get_db()
    user_rows = await legacy_db.execute_fetchall(
        """
        SELECT id, phone, registered_via, created_at, last_login_at
        FROM users
        ORDER BY id
        """
    )
    for row in user_rows:
        await reconcile_authenticated_user(legacy_auth.public_user(row))

    for api_key in settings.api_keys:
        await reconcile_api_key(api_key)

    local_count = 0
    if not settings.auth_required and not settings.api_keys:
        await reconcile_local_anonymous()
        local_count = 1

    return {
        "users_reconciled": len(user_rows),
        "api_keys_reconciled": len(settings.api_keys),
        "local_anonymous_reconciled": local_count,
    }


async def main() -> None:
    try:
        report = await reconcile()
        print(json.dumps({"status": "success", **report}, sort_keys=True))
    finally:
        await legacy_auth.close_db()
        from app.db import close_database

        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
