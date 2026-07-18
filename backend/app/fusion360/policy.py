"""Risk, approval and deterministic hashing policy for Fusion actions."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from .contract import CadAction
from .errors import FusionConnectorError

Risk = Literal["low", "medium", "high"]
Phase = Literal["preview", "execute"]

RISK_BY_ACTION: dict[str, Risk] = {
    "cad.update_parameter": "medium",
    "cad.update_feature_parameter": "medium",
    "cad.create_sketch": "medium",
    "cad.create_extrude": "medium",
    "cad.create_hole": "medium",
    "cad.create_fillet": "medium",
    "cad.create_chamfer": "medium",
    "cad.update_entity_properties": "medium",
    "cad.save_document": "high",
    "cad.save_as": "high",
    "cad.export": "low",
}

_REQUEST_FIELDS = {
    "request_id", "idempotency_key", "timeout_ms", "execution_mode",
    "approval_id", "connector_instance_id",
}


def classify_risk(action_name: str) -> Risk:
    try:
        return RISK_BY_ACTION[action_name]
    except KeyError as exc:
        raise FusionConnectorError("UNSUPPORTED_ACTION", f"Unsupported CAD action: {action_name}") from exc


def requires_approval(action_name: str) -> bool:
    return classify_risk(action_name) in {"medium", "high"}


def _number(value: int | float | Decimal) -> str:
    if isinstance(value, bool):
        raise TypeError("boolean is not a number in canonical encoder")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("CAD-C14N-1 rejects non-finite numbers")
    decimal = Decimal(str(value))
    if not decimal.is_finite():
        raise ValueError("CAD-C14N-1 rejects non-finite numbers")
    if decimal == 0:
        return "0"
    rendered = format(decimal, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def canonical_json(value: Any) -> bytes:
    """Encode the deliberately narrow CAD-C14N-1 format from the contract."""

    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, (int, float, Decimal)):
            return _number(item)
        if isinstance(item, str):
            normalized = unicodedata.normalize("NFC", item)
            return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(v) for v in item) + "]"
        if isinstance(item, dict):
            normalized: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                if not isinstance(raw_key, str):
                    raise TypeError("CAD-C14N-1 object keys must be strings")
                key = unicodedata.normalize("NFC", raw_key)
                if key in normalized:
                    raise ValueError("Unicode normalization produced a duplicate object key")
                normalized[key] = raw_value
            return "{" + ",".join(
                f"{encode(key)}:{encode(normalized[key])}" for key in sorted(normalized)
            ) + "}"
        if isinstance(item, BaseModel):
            return encode(item.model_dump(mode="json"))
        raise TypeError(f"unsupported CAD-C14N-1 value: {type(item).__name__}")

    return encode(value).encode("utf-8")


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def action_intent_payload(action: CadAction) -> dict[str, Any]:
    payload = action.model_dump(mode="json")
    return {key: value for key, value in payload.items() if key not in _REQUEST_FIELDS}


def action_intent_hash(action: CadAction) -> str:
    return sha256_canonical(action_intent_payload(action))


def idempotency_payload_hash(owner_id: str, phase: Phase, intent_hash: str) -> str:
    return sha256_canonical({"owner_id": owner_id, "phase": phase, "action_intent_hash": intent_hash})
