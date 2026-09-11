"""Frozen host contracts for native FreeCAD Assembly BOM execution."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenBOMContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FreeCADBOMComponentV1(FrozenBOMContract):
    step_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,119}$")
    label: str = Field(min_length=1, max_length=240)
    position_mm: tuple[float, float, float] = Field(
        description=(
            "Target position for the centre of the component's axis-aligned "
            "bottom bounding-box face (x/y centre, z minimum)."
        )
    )
    artifact_id: str = Field(
        pattern=r"^component:[a-z0-9][a-z0-9_-]{0,119}$"
    )
    quantity: Literal[1] = 1


class FreeCADBOMRequestV1(FrozenBOMContract):
    schema_version: Literal["freecad-bom-request.v1"] = (
        "freecad-bom-request.v1"
    )
    candidate_build_id: UUID
    base_revision_id: UUID
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    combine_step_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,119}$")
    components: tuple[FreeCADBOMComponentV1, ...] = Field(
        min_length=1,
        max_length=10_000,
    )
    property_columns: tuple[str, ...] = Field(max_length=60)

    @model_validator(mode="after")
    def unique_components_and_columns(self) -> "FreeCADBOMRequestV1":
        keys = [component.step_key for component in self.components]
        if len(keys) != len(set(keys)):
            raise ValueError("BOM component step keys must be unique")
        if len(self.property_columns) != len(set(self.property_columns)):
            raise ValueError("BOM property columns must be unique")
        return self


class FreeCADBOMSourceArtifactV1(FrozenBOMContract):
    staging_manifest_id: UUID
    artifact_role: Literal["step"] = "step"
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FreeCADBOMSourceComponentV1(FreeCADBOMSourceArtifactV1):
    step_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,119}$")


class FreeCADBOMRowV1(FrozenBOMContract):
    index: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    quantity: int = Field(gt=0)
    file_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
    properties: dict[str, str] = Field(default_factory=dict)


class FreeCADBOMDocumentV1(FrozenBOMContract):
    schema_version: Literal["freecad-bom.v1"] = "freecad-bom.v1"
    source: dict
    generator: dict
    columns: tuple[str, ...] = Field(min_length=4, max_length=64)
    rows: tuple[FreeCADBOMRowV1, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_generator(self) -> "FreeCADBOMDocumentV1":
        if self.generator.get("freecad_version") != "1.1.3":
            raise ValueError("BOM generator FreeCAD version mismatch")
        if self.generator.get("native_type") != "Assembly::BomObject":
            raise ValueError("BOM generator is not native Assembly::BomObject")
        return self
