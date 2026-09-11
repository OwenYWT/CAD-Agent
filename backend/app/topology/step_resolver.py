from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from app.validation.verification.contracts import VerificationTarget

from .contracts import FeatureBinding, TopologyResolution
from .resolver import TopologyResolver


def resolve_step_topology(
    target: VerificationTarget,
    *,
    feature_id: str | None = None,
    backend: Literal["cadquery", "fusion", "onshape", "step"] = "step",
    backend_object_id: str | None = None,
    revision_id: str | None = None,
    evidence_ref: str | None = None,
) -> TopologyResolution:
    resolver = TopologyResolver()
    return resolver.resolve_target(
        target,
        feature_id=feature_id,
        backend=backend,
        backend_object_id=backend_object_id,
        revision_id=revision_id,
        evidence_ref=evidence_ref,
    )


def bind_step_feature(
    *,
    feature_id: str,
    target_ids: Sequence[str],
    backend: Literal["cadquery", "fusion", "onshape", "step"] = "step",
    backend_object_id: str,
    revision_id: str | None = None,
    selector=None,
    notes: Sequence[str] = (),
) -> FeatureBinding:
    resolver = TopologyResolver()
    return resolver.bind_feature(
        feature_id=feature_id,
        backend=backend,
        backend_object_id=backend_object_id,
        selector=selector,
        revision_id=revision_id,
        verification_target_ids=tuple(target_ids),
        notes=tuple(notes),
    )
