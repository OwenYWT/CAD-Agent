"""Validated host-side contract for immutable FreeCAD state artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.object_store import get_object
from app.parameters import CADParameter
from app.freecad.contracts import FreeCADOperationPlan


_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_PROPERTY_UNITS = {
    "App::PropertyLength": "mm",
    "App::PropertyDistance": "mm",
    "App::PropertyQuantityConstraint": "mm",
    "App::PropertyAngle": "deg",
    "App::PropertyFloat": None,
    "App::PropertyInteger": None,
}


class ParameterStateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FreeCADStateParameterV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=161)
    object_name: str
    property_name: str
    label: str = Field(min_length=1, max_length=500)
    group: str = Field(max_length=200)
    property_type: Literal[
        "App::PropertyLength",
        "App::PropertyDistance",
        "App::PropertyQuantityConstraint",
        "App::PropertyAngle",
        "App::PropertyFloat",
        "App::PropertyInteger",
    ]
    value: int | float
    unit: Literal["mm", "deg"] | None
    editable: bool
    minimum: int | float | None = None
    maximum: int | float | None = None
    step: int | float | None = None

    @field_validator("object_name", "property_name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if _NAME.fullmatch(value) is None:
            raise ValueError("parameter object/property name is not safe")
        return value

    @field_validator("value", "minimum", "maximum", "step")
    @classmethod
    def finite_numeric(cls, value: int | float | None):
        if value is None:
            return value
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("parameter numeric value must be finite")
        return value

    @model_validator(mode="after")
    def consistent(self) -> "FreeCADStateParameterV2":
        if self.id != f"{self.object_name}.{self.property_name}":
            raise ValueError("parameter id does not match object/property")
        if self.unit != _PROPERTY_UNITS[self.property_type]:
            raise ValueError("parameter unit does not match property type")
        if self.property_type == "App::PropertyInteger" and float(self.value) % 1:
            raise ValueError("integer parameter value must be integral")
        if self.minimum is not None and self.maximum is not None:
            if float(self.minimum) > float(self.maximum):
                raise ValueError("parameter bounds are reversed")
        if self.step is not None and float(self.step) <= 0:
            raise ValueError("parameter step must be positive")
        return self

    def to_cad_parameter(self) -> CADParameter:
        return CADParameter(
            name=self.id,
            display_name=self.label,
            value=float(self.value),
            default_value=float(self.value),
            min=float(self.minimum) if self.minimum is not None else None,
            max=float(self.maximum) if self.maximum is not None else None,
            step=float(self.step) if self.step is not None else None,
            unit=self.unit,
            group=self.group or None,
            source="freecad",
            object_name=self.object_name,
            property_name=self.property_name,
            property_type=self.property_type,
        )


class FreeCADStateV2(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: Literal["freecad-state.v2"]
    document: str
    object_count: int = Field(ge=0)
    root_objects: list[str]
    objects: list[dict[str, Any]]
    parameters: list[FreeCADStateParameterV2]

    @model_validator(mode="after")
    def unique_parameters(self) -> "FreeCADStateV2":
        identifiers = [parameter.id for parameter in self.parameters]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("FreeCAD state contains duplicate parameter IDs")
        return self


async def read_verified_state_artifact(
    artifacts: list[dict[str, Any]],
    *,
    require_v2: bool = False,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    eligible = [row for row in artifacts if row.get("artifact_kind") == "state"]
    if len(eligible) != 1:
        raise ParameterStateError(
            "parameter_state_missing",
            "base revision must contain exactly one FreeCAD state artifact",
        )
    artifact = eligible[0]
    sha256 = str(artifact.get("sha256") or "")
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise ParameterStateError(
            "parameter_state_stale",
            "parameter state hash differs from the committed revision",
        )
    payload = await get_object(str(artifact["object_key"]))
    if (
        len(payload) != int(artifact["size_bytes"])
        or hashlib.sha256(payload).hexdigest() != sha256
    ):
        raise ParameterStateError(
            "parameter_state_missing",
            "FreeCAD state artifact failed integrity verification",
        )
    try:
        state = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParameterStateError(
            "parameter_state_missing",
            "FreeCAD state artifact is not valid JSON",
        ) from exc
    if not isinstance(state, dict) or state.get("schema_version") not in {
        "freecad-state.v1",
        "freecad-state.v2",
    }:
        raise ParameterStateError(
            "parameter_state_missing",
            "FreeCAD state artifact has an unsupported schema",
        )
    if require_v2 and state.get("schema_version") != "freecad-state.v2":
        raise ParameterStateError(
            "parameter_state_missing",
            "structured parameter editing requires freecad-state.v2",
        )
    if state.get("schema_version") == "freecad-state.v2":
        state = FreeCADStateV2.model_validate(state).model_dump(mode="json")
    return state, sha256


def project_state_parameters(state: dict[str, Any]) -> list[CADParameter]:
    if state.get("schema_version") != "freecad-state.v2":
        return []
    validated = FreeCADStateV2.model_validate(state)
    return [parameter.to_cad_parameter() for parameter in validated.parameters]


def compile_parameter_operation_plan(
    state: dict[str, Any],
    modification: dict[str, Any],
    *,
    output_formats: tuple[str, ...],
) -> FreeCADOperationPlan:
    validated = FreeCADStateV2.model_validate(state)
    by_id = {parameter.id: parameter for parameter in validated.parameters}
    raw_updates = modification.get("parameter_updates")
    if not isinstance(raw_updates, (list, tuple)) or not raw_updates:
        raise ParameterStateError(
            "parameter_value_type_invalid",
            "parameter update batch must be non-empty",
        )
    identifiers = [
        str(item.get("parameter_id") or "")
        for item in raw_updates
        if isinstance(item, dict)
    ]
    if len(identifiers) != len(raw_updates):
        raise ParameterStateError(
            "parameter_value_type_invalid",
            "parameter update must be an object",
        )
    if len(identifiers) != len(set(identifiers)):
        raise ParameterStateError(
            "parameter_duplicate_update",
            "parameter update batch contains a duplicate ID",
        )
    operations: list[dict[str, Any]] = []
    for update in sorted(raw_updates, key=lambda item: str(item["parameter_id"])):
        identifier = str(update["parameter_id"])
        parameter = by_id.get(identifier)
        if parameter is None or not parameter.editable:
            raise ParameterStateError(
                "parameter_not_editable",
                f"parameter is not editable: {identifier}",
            )
        value = update.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ParameterStateError(
                "parameter_value_type_invalid",
                f"parameter value is invalid: {identifier}",
            )
        numeric = float(value)
        if parameter.property_type == "App::PropertyInteger" and (
            not numeric.is_integer() or not -(2**31) <= numeric < 2**31
        ):
            raise ParameterStateError(
                "parameter_value_type_invalid",
                f"integer parameter is not representable: {identifier}",
            )
        if parameter.minimum is not None and numeric < float(parameter.minimum):
            raise ParameterStateError(
                "parameter_value_out_of_range",
                f"parameter is below its committed minimum: {identifier}",
            )
        if parameter.maximum is not None and numeric > float(parameter.maximum):
            raise ParameterStateError(
                "parameter_value_out_of_range",
                f"parameter exceeds its committed maximum: {identifier}",
            )
        op_hash = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]
        operations.append({
            "op_id": f"set-parameter-{op_hash}",
            "action": "property.set",
            "args": {
                "object": parameter.object_name,
                "property": parameter.property_name,
                "value": int(numeric)
                if parameter.property_type == "App::PropertyInteger"
                else numeric,
                "expected_property_type": parameter.property_type,
                "unit": parameter.unit,
            },
        })
    exports = tuple(dict.fromkeys(("fcstd", *output_formats)))
    operations.append({
        "op_id": "export-structured-parameters",
        "action": "document.export",
        "args": {"formats": list(exports), "basename": "model"},
    })
    return FreeCADOperationPlan(
        document_name="Model",
        operations=tuple(operations),
    )
