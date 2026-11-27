"""Shared route-aware CADTest predicate for machine and HTML reports."""

from typing import Any, Mapping


def native_delivery_verified(run: Mapping[str, Any]) -> bool:
    score = run.get("cad_test")
    delivery = score.get("delivery") if isinstance(score, Mapping) else None
    gate = run.get("artifact_gate")
    if isinstance(gate, Mapping) and "native_delivery_verified" in gate:
        return gate.get("native_delivery_verified") is True and not (
            isinstance(delivery, Mapping) and delivery.get("status") == "failed"
        )
    return isinstance(delivery, Mapping) and delivery.get("status") == "passed"


def cad_test_delivery_passed(run: Mapping[str, Any]) -> bool:
    score = run.get("cad_test")
    if (
        not isinstance(score, Mapping)
        or score.get("status") != "passed"
        or score.get("verdict") is not True
    ):
        return False
    gate = run.get("artifact_gate")
    if isinstance(gate, Mapping) and "dxf_readable" in gate:
        delivery = score.get("delivery")
        return gate.get("dxf_readable") is True and not (
            isinstance(delivery, Mapping) and delivery.get("status") == "failed"
        )
    return native_delivery_verified(run)
