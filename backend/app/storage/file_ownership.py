"""Request-scoped ownership metadata for legacy generated CAD artifacts."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path

from app.config import settings


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OWNER_FILE = ".owner"


class FileOwnershipError(RuntimeError):
    pass


def _request_dir(request_id: str) -> Path:
    if not _SAFE_ID.fullmatch(request_id):
        raise FileOwnershipError("Invalid request ID")
    root = Path(settings.file_storage_dir).expanduser().resolve()
    target = (root / request_id).resolve()
    if target.parent != root:
        raise FileOwnershipError("Invalid request path")
    return target


def _fingerprint(principal: str) -> str:
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


def claim_request_owner(request_id: str | None, principal: str | None) -> None:
    """Bind an existing request directory to a login user or legacy API key."""
    if not request_id or not isinstance(principal, str) or not principal:
        return
    request_dir = _request_dir(request_id)
    if not request_dir.is_dir():
        return
    marker = request_dir / _OWNER_FILE
    expected = _fingerprint(principal)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        actual = marker.read_text(encoding="ascii").strip()
        if not hmac.compare_digest(actual, expected):
            raise FileOwnershipError("Request artifacts already belong to another user")
        return
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(expected)


def request_belongs_to(request_id: str, principal: str | None) -> bool:
    """Auth-off development remains compatible; authenticated mode is fail-closed."""
    if not isinstance(principal, str):
        principal = None
    if principal is None:
        return not settings.auth_required and not settings.api_keys
    marker = _request_dir(request_id) / _OWNER_FILE
    try:
        actual = marker.read_text(encoding="ascii").strip()
    except OSError:
        return False
    return hmac.compare_digest(actual, _fingerprint(principal))
