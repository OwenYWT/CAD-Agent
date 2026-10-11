"""A cut repair cannot carry dimensions or unrelated modeling operations."""
from typing import Literal

from pydantic import model_validator

from app.contracts.constraint_patch import Hash
from app.freecad.contracts import Coordinate, FrozenContract, OperationId, SketchFrame


class SketchPlacement(FrozenContract):
    plane: Literal['xy', 'xz', 'yz'] = 'xy'
    offset_mm: Coordinate = 0.0
    reversed: bool = False
    frame: SketchFrame | None = None


class CutPatch(FrozenContract):
    schema_version: Literal['freecad-cut-patch.v1'] = 'freecad-cut-patch.v1'
    plan_hash: Hash
    operation_id: OperationId
    reversed: bool | None = None
    placement: SketchPlacement | None = None

    @model_validator(mode='after')
    def has_change(self):
        if self.reversed is None and self.placement is None:
            raise ValueError('a cut patch requires direction or private sketch placement')
        return self
