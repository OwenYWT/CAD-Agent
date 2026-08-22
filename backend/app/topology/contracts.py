from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.geometry_ir.contracts import Axis


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SurfaceType(str, Enum):
    PLANE = "plane"
    CYLINDER = "cylinder"
    CONE = "cone"
    SPHERE = "sphere"
    TORUS = "torus"
    CUSTOM = "custom"


class CurveType(str, Enum):
    LINE = "line"
    CIRCLE = "circle"
    ELLIPSE = "ellipse"
    SPLINE = "spline"
    HELIX = "helix"
    CUSTOM = "custom"


class TopologySelector(FrozenContract):
    entity_type: Literal["face", "edge"]
    surface_type: SurfaceType | None = None
    curve_type: CurveType | None = None
    axis: Axis | None = None
    radius_range: tuple[float, float] | None = None
    expected_position: tuple[float, float, float] | None = None
    expected_normal: tuple[float, float, float] | None = None
    sort_by: str | None = Field(default=None, max_length=120)
    index: int | Literal["all"] = 0
    resolution_state: Literal["hinted", "resolved", "unresolved"] = "hinted"
    confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    notes: tuple[str, ...] = ()


class BackendObjectRef(FrozenContract):
    backend: Literal["cadquery", "fusion", "onshape", "step"]
    object_id: str = Field(min_length=1, max_length=255)
    role: str = Field(default="feature", min_length=1, max_length=120)
    revision_id: str | None = Field(default=None, min_length=1, max_length=120)
    notes: tuple[str, ...] = ()


class FeatureBinding(FrozenContract):
    feature_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    backend: Literal["cadquery", "fusion", "onshape", "step"]
    backend_object_id: str = Field(min_length=1, max_length=255)
    selector: TopologySelector | None = None
    revision_id: str | None = Field(default=None, min_length=1, max_length=120)
    resolution_state: Literal["resolved", "hinted", "unresolved"] = "hinted"
    verification_target_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


class TopologyResolution(FrozenContract):
    target_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    feature_id: str | None = Field(default=None, min_length=1, max_length=120)
    selector: TopologySelector | None = None
    backend_object: BackendObjectRef | None = None
    resolution_state: Literal["resolved", "hinted", "unresolved"] = "hinted"
    notes: tuple[str, ...] = ()

