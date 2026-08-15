"""RFC 8785 canonical JSON used for execution idempotency and audit hashes."""

from __future__ import annotations

import hashlib
import math
import unicodedata
from decimal import Decimal
from enum import Enum
from typing import Any

import rfc8785
from pydantic import BaseModel


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(
            mode="json",
            exclude_none=False,
            exclude_unset=False,
            exclude_defaults=False,
        )
    if isinstance(value, Enum):
        return _normalize(value.value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Decimal):
        raise TypeError("Decimal values must be encoded as explicit decimal strings")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON numbers must be finite")
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError(
                    f"canonical JSON key collision after Unicode NFC: {normalized_key!r}"
                )
            normalized[normalized_key] = _normalize(item)
        return normalized
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value as normalized RFC 8785 bytes."""
    return rfc8785.dumps(_normalize(value))


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
