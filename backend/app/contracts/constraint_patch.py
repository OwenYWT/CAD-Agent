"""Scoped Agent proposals. No approval, provenance, acceptance or native ordinals."""
from typing import Annotated, Literal, Protocol, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.freecad.contracts import SketchAddConstraintArgs


Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
LogicalId = Annotated[str, Field(min_length=1, max_length=160, pattern=r'^[a-z0-9][a-z0-9_-]*$')]


class PatchModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ConstraintRepairValidation(Protocol):
    """Execution consumes this port; CAD supplies the implementation at composition."""
    def validate_snapshot(self, evidence: dict, plan: dict, checkpoint_hash: str | None,
                          failed_operation_id: str) -> dict: ...
    def verify_contract(self, plan: Any, certificate: dict, acceptance: Any) -> None: ...
    def verify_receipt(self, metadata: dict, certificate: dict) -> dict: ...


class ConstraintDefinition(PatchModel):
    kind: SketchAddConstraintArgs.model_fields['kind'].annotation
    first: SketchAddConstraintArgs.model_fields['first'].annotation
    second: SketchAddConstraintArgs.model_fields['second'].annotation = None
    value_mm: SketchAddConstraintArgs.model_fields['value_mm'].annotation = None
    driving: bool = True

    @model_validator(mode='after')
    def native_signature(self):
        SketchAddConstraintArgs.model_validate({'sketch': 'ValidationTarget',
            **self.model_dump(exclude={'driving'})})
        if not self.driving and self.value_mm is None:
            raise ValueError('only dimensional constraints can become reference measurements')
        return self


class AddConstraint(PatchModel):
    action: Literal['add']
    logical_id: LogicalId
    constraint: ConstraintDefinition


class DeleteConstraint(PatchModel):
    action: Literal['delete']
    logical_id: LogicalId


class ReplaceConstraint(PatchModel):
    action: Literal['replace']
    logical_id: LogicalId
    constraint: ConstraintDefinition


class ConstraintPatch(PatchModel):
    schema_version: Literal['freecad-constraint-patch.v1'] = 'freecad-constraint-patch.v1'
    plan_hash: Hash
    checkpoint_hash: Hash | None
    diagnostic_hash: Hash
    sketch: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$', max_length=80)
    changes: tuple[Annotated[AddConstraint | DeleteConstraint | ReplaceConstraint,
        Field(discriminator='action')], ...] = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def unique_changes(self):
        ids = [change.logical_id for change in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError('a patch can change each logical constraint at most once')
        return self
