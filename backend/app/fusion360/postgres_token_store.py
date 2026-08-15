"""Tenant-safe PostgreSQL persistence for Autodesk OAuth secrets."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from app.db import auth_transaction, tenant_transaction
from app.principal_context import current_principal

from .errors import FusionConnectorError


class PostgresEncryptedTokenStore:
    """Encrypted token storage with a cross-tenant, one-time OAuth callback gate."""

    def __init__(self, encryption_key: str | bytes):
        if not encryption_key:
            raise RuntimeError("A deployment-provided Fernet key is required")
        raw_key = (
            encryption_key.encode()
            if isinstance(encryption_key, str)
            else encryption_key
        )
        try:
            self._fernet = Fernet(raw_key)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "FUSION_TOKEN_ENCRYPTION_KEY is not a valid Fernet key"
            ) from exc
        self._key_id = hashlib.sha256(raw_key).hexdigest()[:16]

    def _encrypt_json(self, value: dict[str, Any]) -> bytes:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._fernet.encrypt(payload)

    def _decrypt_json(self, payload: bytes) -> dict[str, Any]:
        try:
            value = json.loads(self._fernet.decrypt(payload).decode("utf-8"))
        except (InvalidToken, UnicodeError, json.JSONDecodeError) as exc:
            raise FusionConnectorError(
                "CLOUD_AUTH_REQUIRED",
                "Stored Autodesk credentials cannot be decrypted",
            ) from exc
        if not isinstance(value, dict):
            raise FusionConnectorError(
                "CLOUD_AUTH_REQUIRED",
                "Stored Autodesk credentials are invalid",
            )
        return value

    async def put(
        self,
        owner_id: str,
        token: dict[str, Any],
        *,
        now: float | None = None,
    ) -> None:
        del owner_id, now
        context = current_principal()
        encrypted = self._encrypt_json(token)
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_tokens (
                        tenant_id, principal_id, connector, encrypted_token,
                        encryption_key_id
                    ) VALUES (
                        :tenant, :principal, 'fusion360', :token, :key_id
                    )
                    ON CONFLICT (tenant_id, principal_id, connector)
                    DO UPDATE SET
                        encrypted_token=EXCLUDED.encrypted_token,
                        encryption_key_id=EXCLUDED.encryption_key_id,
                        updated_at=CURRENT_TIMESTAMP
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "token": encrypted,
                    "key_id": self._key_id,
                },
            )

    async def get(self, owner_id: str) -> dict[str, Any] | None:
        del owner_id
        context = current_principal()
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            encrypted = await connection.scalar(
                text(
                    """
                    SELECT encrypted_token
                    FROM connector_tokens
                    WHERE tenant_id=:tenant AND principal_id=:principal
                      AND connector='fusion360'
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                },
            )
        return self._decrypt_json(bytes(encrypted)) if encrypted is not None else None

    async def delete(self, owner_id: str) -> None:
        del owner_id
        context = current_principal()
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM connector_tokens
                    WHERE tenant_id=:tenant AND principal_id=:principal
                      AND connector='fusion360'
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                },
            )

    async def create_state(
        self,
        state: str,
        owner_id: str,
        verifier: str,
        *,
        ttl_s: float = 600,
        now: float | None = None,
    ) -> None:
        context = current_principal()
        timestamp = now if now is not None else datetime.now(timezone.utc).timestamp()
        expires_at = datetime.fromtimestamp(
            timestamp + min(max(ttl_s, 60), 600),
            timezone.utc,
        )
        encrypted = self._encrypt_json(
            {
                "owner_id": owner_id,
                "verifier": verifier,
                "tenant_id": str(context.tenant_id),
                "principal_id": str(context.principal_id),
            }
        )
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_oauth_states (
                        tenant_id, principal_id, connector, state,
                        encrypted_verifier, expires_at
                    ) VALUES (
                        :tenant, :principal, 'fusion360', :state,
                        :verifier, :expires
                    )
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                    "state": state,
                    "verifier": encrypted,
                    "expires": expires_at,
                },
            )

    async def consume_state(
        self,
        state: str,
        *,
        now: float | None = None,
    ) -> tuple[str, str]:
        timestamp = now if now is not None else datetime.now(timezone.utc).timestamp()
        consumed_at = datetime.fromtimestamp(timestamp, timezone.utc)
        async with auth_transaction() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT tenant_id, principal_id, encrypted_verifier,
                               expires_at, consumed_at
                        FROM connector_oauth_states
                        WHERE connector='fusion360' AND state=:state
                        FOR UPDATE
                        """
                    ),
                    {"state": state},
                )
            ).mappings().one_or_none()
            if (
                row is None
                or row["consumed_at"] is not None
                or row["expires_at"] < consumed_at
            ):
                raise FusionConnectorError(
                    "CLOUD_AUTH_REQUIRED",
                    "OAuth state is invalid, expired, or already used",
                )
            await connection.execute(
                text(
                    """
                    UPDATE connector_oauth_states
                    SET consumed_at=:consumed
                    WHERE tenant_id=:tenant AND connector='fusion360'
                      AND state=:state AND consumed_at IS NULL
                    """
                ),
                {
                    "consumed": consumed_at,
                    "tenant": row["tenant_id"],
                    "state": state,
                },
            )
        payload = self._decrypt_json(bytes(row["encrypted_verifier"]))
        return str(payload["owner_id"]), str(payload["verifier"])

    async def put_after_state(
        self,
        state: str,
        token: dict[str, Any],
    ) -> None:
        """Persist the exchanged token to the principal bound to ``state``."""
        async with auth_transaction() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT tenant_id, principal_id, encrypted_verifier
                        FROM connector_oauth_states
                        WHERE connector='fusion360' AND state=:state
                          AND consumed_at IS NOT NULL
                        """
                    ),
                    {"state": state},
                )
            ).mappings().one_or_none()
        if row is None:
            raise FusionConnectorError(
                "CLOUD_AUTH_REQUIRED",
                "OAuth state is not available for token storage",
            )
        encrypted = self._encrypt_json(token)
        async with tenant_transaction(
            row["tenant_id"],
            row["principal_id"],
        ) as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO connector_tokens (
                        tenant_id, principal_id, connector, encrypted_token,
                        encryption_key_id
                    ) VALUES (
                        :tenant, :principal, 'fusion360', :token, :key_id
                    )
                    ON CONFLICT (tenant_id, principal_id, connector)
                    DO UPDATE SET
                        encrypted_token=EXCLUDED.encrypted_token,
                        encryption_key_id=EXCLUDED.encryption_key_id,
                        updated_at=CURRENT_TIMESTAMP
                    """
                ),
                {
                    "tenant": row["tenant_id"],
                    "principal": row["principal_id"],
                    "token": encrypted,
                    "key_id": self._key_id,
                },
            )

    async def encrypted_bytes(self, owner_id: str) -> bytes | None:
        del owner_id
        context = current_principal()
        async with tenant_transaction(
            context.tenant_id,
            context.principal_id,
        ) as connection:
            value = await connection.scalar(
                text(
                    """
                    SELECT encrypted_token
                    FROM connector_tokens
                    WHERE tenant_id=:tenant AND principal_id=:principal
                      AND connector='fusion360'
                    """
                ),
                {
                    "tenant": context.tenant_id,
                    "principal": context.principal_id,
                },
            )
        return bytes(value) if value is not None else None
