"""Local boundary proposals; the backend owns the enclosing CAD transaction."""
from typing import Literal

from pydantic import Field

from app.contracts.constraint_patch import Hash
from app.freecad.contracts import FreeCADOperation, FrozenContract, ObjectName


class ProfileOperation(FreeCADOperation):
    action: Literal['sketch.add_geometry', 'sketch.add_profile', 'sketch.add_constraint']


class ProfilePatch(FrozenContract):
    schema_version: Literal['freecad-profile-patch.v1'] = 'freecad-profile-patch.v1'
    plan_hash: Hash
    checkpoint_hash: Hash | None
    diagnostic_hash: Hash
    sketch: ObjectName
    operations: tuple[ProfileOperation, ...] = Field(min_length=1, max_length=200)
