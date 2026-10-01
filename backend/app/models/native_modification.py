"""Native modification wire models shared by HTTP and durable execution."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer
from app.freecad.contracts import FreeCADOperation

class FreeCADParameterUpdateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter_id: str = Field(
        min_length=3,
        max_length=161,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}\.[A-Za-z_][A-Za-z0-9_]{0,79}$",
    )
    value: float = Field(allow_inf_nan=False, strict=True)


class FreeCADNativeEditV1(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    action: Literal['assembly.instance', 'assembly.place', 'sketch.set_constraint']
    args: dict

    @model_validator(mode='after')
    def typed_arguments(self):
        operation = FreeCADOperation(op_id='validate', action=self.action, args=self.args)
        object.__setattr__(self, 'args', operation.args)
        return self


class FreeCADStructuredModificationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["freecad-structured-modification.v1"] = (
        "freecad-structured-modification.v1"
    )
    expected_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parameter_updates: tuple[FreeCADParameterUpdateV1, ...] = Field(
        default=(),
        min_length=0,
        max_length=100,
    )
    native_edits: tuple[FreeCADNativeEditV1, ...] = Field(default=(), max_length=20)

    @model_serializer(mode='wrap')
    def historical_wire_identity(self, handler):
        payload = handler(self)
        if not self.native_edits:
            payload.pop('native_edits', None)
        return payload

    @model_validator(mode="after")
    def unique_updates(self) -> "FreeCADStructuredModificationV1":
        if bool(self.parameter_updates) == bool(self.native_edits):
            raise ValueError('provide either parameter_updates or native_edits')
        identifiers = [update.parameter_id for update in self.parameter_updates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("parameter_duplicate_update")
        return self
