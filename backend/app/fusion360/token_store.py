"""Encrypted, owner-isolated APS OAuth token and one-time state storage."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .errors import FusionConnectorError


class EncryptedTokenStore:
    def __init__(self, path: str | Path, encryption_key: str | bytes):
        if not encryption_key:
            raise RuntimeError("A deployment-provided Fernet key is required")
        try:
            self._fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("FUSION_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                owner_id TEXT PRIMARY KEY,
                encrypted_token BLOB NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                encrypted_verifier BLOB NOT NULL,
                expires_at REAL NOT NULL,
                consumed_at REAL
            );
            """
        )

    def put(self, owner_id: str, token: dict[str, Any], *, now: float | None = None) -> None:
        now = now or time.time()
        payload = json.dumps(token, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        encrypted = self._fernet.encrypt(payload)
        with self._lock:
            self._db.execute(
                """INSERT INTO oauth_tokens VALUES (?,?,?)
                ON CONFLICT(owner_id) DO UPDATE SET encrypted_token=excluded.encrypted_token,updated_at=excluded.updated_at""",
                (owner_id, encrypted, now),
            )

    def get(self, owner_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT encrypted_token FROM oauth_tokens WHERE owner_id=?", (owner_id,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(self._fernet.decrypt(row["encrypted_token"]).decode("utf-8"))
        except (InvalidToken, UnicodeError, json.JSONDecodeError) as exc:
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "Stored Autodesk credentials cannot be decrypted") from exc

    def delete(self, owner_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM oauth_tokens WHERE owner_id=?", (owner_id,))

    def create_state(self, state: str, owner_id: str, verifier: str, *, ttl_s: float = 600, now: float | None = None) -> None:
        now = now or time.time()
        encrypted = self._fernet.encrypt(verifier.encode("ascii"))
        with self._lock:
            self._db.execute(
                "INSERT INTO oauth_states VALUES (?,?,?,?,NULL)",
                (state, owner_id, encrypted, now + min(max(ttl_s, 60), 600)),
            )

    def consume_state(self, state: str, *, now: float | None = None) -> tuple[str, str]:
        now = now or time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone()
                if not row or row["consumed_at"] is not None or row["expires_at"] < now:
                    raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "OAuth state is invalid, expired, or already used")
                self._db.execute("UPDATE oauth_states SET consumed_at=? WHERE state=? AND consumed_at IS NULL", (now, state))
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        try:
            verifier = self._fernet.decrypt(row["encrypted_verifier"]).decode("ascii")
        except (InvalidToken, UnicodeError) as exc:
            raise FusionConnectorError("CLOUD_AUTH_REQUIRED", "OAuth verifier cannot be decrypted") from exc
        return row["owner_id"], verifier

    def encrypted_bytes(self, owner_id: str) -> bytes | None:
        """Diagnostic/test accessor; never expose through an API."""
        with self._lock:
            row = self._db.execute("SELECT encrypted_token FROM oauth_tokens WHERE owner_id=?", (owner_id,)).fetchone()
        return bytes(row["encrypted_token"]) if row else None
