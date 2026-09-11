"""Persistable semantic selectors; raw FaceN/EdgeN names are intentionally absent."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenTopologyContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SelectorPoint3D(FrozenTopologyContract):
    x: float = Field(ge=-1_000_000, le=1_000_000)
    y: float = Field(ge=-1_000_000, le=1_000_000)
    z: float = Field(ge=-1_000_000, le=1_000_000)


class FreeCADTopologySelector(FrozenTopologyContract):
    schema_version: Literal["topology-selector.v1"] = "topology-selector.v1"
    backend: Literal["freecad"] = "freecad"
    revision_id: UUID
    object_name: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    subelement_kind: Literal["face", "edge"]
    geometry: Literal["planar", "circular"]
    axis: Literal["x", "y", "z"]
    extreme: Literal["min", "max"]
    normal_sign: Literal[-1, 1] | None = None
    radius_mm: float | None = Field(default=None, gt=0, le=1_000_000)
    center: SelectorPoint3D | None = None
    tolerance_mm: float = Field(default=1e-5, gt=0, le=10)

    @model_validator(mode="after")
    def compatible_geometry(self) -> "FreeCADTopologySelector":
        if self.subelement_kind == "face" and self.geometry != "planar":
            raise ValueError("face selectors currently require planar geometry")
        if self.subelement_kind == "edge" and self.geometry != "circular":
            raise ValueError("edge selectors currently require circular geometry")
        if self.geometry == "planar" and self.radius_mm is not None:
            raise ValueError("planar selectors do not accept radius_mm")
        if self.geometry == "circular" and self.radius_mm is None:
            raise ValueError("circular selectors require radius_mm")
        return self
