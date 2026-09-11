from __future__ import annotations

from typing import Literal

from app.validation.verification.contracts import VerificationTarget, VerificationType

from .contracts import (
    BackendObjectRef,
    CurveType,
    FeatureBinding,
    SurfaceType,
    TopologyResolution,
    TopologySelector,
)


class TopologyResolver:
    def build_selector_hint(self, target: VerificationTarget) -> TopologySelector | None:
        feature_type = target.target_type
        if feature_type in {
            VerificationType.HOLE_DIAMETER,
            VerificationType.HOLE_DEPTH,
            VerificationType.HOLE_POSITION,
            VerificationType.HOLE_DISTANCE,
            VerificationType.COUNTERBORE_DIAMETER,
            VerificationType.COUNTERBORE_DEPTH,
        }:
            return TopologySelector(
                entity_type="face",
                surface_type=SurfaceType.CYLINDER,
                axis=target.measurement_axis,
                sort_by="area_desc",
                index=0,
                resolution_state="hinted",
                confidence=0.45,
                notes=("hole-like target mapped to cylindrical face hint",),
            )
        if feature_type in {VerificationType.FILLET_RADIUS, VerificationType.EDGE_MARGIN}:
            return TopologySelector(
                entity_type="edge",
                curve_type=CurveType.CIRCLE,
                sort_by="radius_asc",
                index=0,
                resolution_state="hinted",
                confidence=0.35,
                notes=("edge-like target mapped to curved edge hint",),
            )
        if feature_type in {VerificationType.OVERALL_DIMENSION, VerificationType.VOLUME}:
            return TopologySelector(
                entity_type="face",
                surface_type=SurfaceType.PLANE,
                index="all",
                resolution_state="unresolved",
                confidence=0.15,
                notes=("planar extent target requires concrete topology measurements",),
            )
        if feature_type in {VerificationType.SINGLE_BODY, VerificationType.WATER_TIGHTNESS}:
            return None
        return TopologySelector(
            entity_type="face",
            resolution_state="unresolved",
            confidence=0.2,
            notes=("no safe selector hint available for this target type",),
        )

    def resolve_target(
        self,
        target: VerificationTarget,
        *,
        feature_id: str | None = None,
        backend: Literal["cadquery", "fusion", "onshape", "step"] = "step",
        backend_object_id: str | None = None,
        revision_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> TopologyResolution:
        selector = self.build_selector_hint(target)
        resolved_feature_id = feature_id or target.feature_id
        if backend_object_id is None:
            backend_object_id = resolved_feature_id or target.target_id
        if selector is None:
            notes = ()
            if evidence_ref:
                notes = (f"evidence_ref={evidence_ref}",)
            return TopologyResolution(
                target_id=target.target_id,
                feature_id=resolved_feature_id,
                selector=None,
                backend_object=BackendObjectRef(
                    backend=backend,
                    object_id=backend_object_id,
                    role="verification-target",
                    revision_id=revision_id,
                    notes=notes,
                ),
                resolution_state="unresolved",
                notes=("no selector hint could be derived",),
            )
        state = "hinted"
        notes = (f"evidence_ref={evidence_ref}",) if evidence_ref else ()
        return TopologyResolution(
            target_id=target.target_id,
            feature_id=resolved_feature_id,
            selector=selector,
            backend_object=BackendObjectRef(
                backend=backend,
                object_id=backend_object_id,
                role="verification-target",
                revision_id=revision_id,
                notes=notes,
            ),
            resolution_state=state,
            notes=(
                f"selector={selector.entity_type}:{selector.resolution_state}",
            ),
        )

    def bind_feature(
        self,
        *,
        feature_id: str,
        backend: Literal["cadquery", "fusion", "onshape", "step"],
        backend_object_id: str,
        selector: TopologySelector | None = None,
        revision_id: str | None = None,
        verification_target_ids: tuple[str, ...] = (),
        notes: tuple[str, ...] = (),
    ) -> FeatureBinding:
        return FeatureBinding(
            feature_id=feature_id,
            backend=backend,
            backend_object_id=backend_object_id,
            selector=selector,
            revision_id=revision_id,
            resolution_state=(
                "resolved"
                if selector is not None and backend in {"fusion", "onshape"}
                else "hinted"
            ),
            verification_target_ids=verification_target_ids,
            notes=notes,
        )
