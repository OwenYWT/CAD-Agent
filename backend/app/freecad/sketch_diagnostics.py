"""Read Sketcher diagnostics; never infer constraint relationships ourselves.

FreeCAD's diagnostic constraint numbers are one-based (unlike mutation indexes).
Keep their native numbering explicit rather than turning them into edit commands.
"""
from __future__ import annotations

from typing import Any


def diagnose_sketch(sketch: Any) -> dict[str, Any]:
    status = int(sketch.solve())
    redundant = [int(i) for i in getattr(sketch, 'RedundantConstraints', ())]
    partial = [int(i) for i in getattr(sketch, 'PartiallyRedundantConstraints', ())]
    conflicting = [int(i) for i in getattr(sketch, 'ConflictingConstraints', ())]
    fully = bool(getattr(sketch, 'FullyConstrained', False))
    constraint_status = (
        'conflicting' if conflicting else
        'redundant' if redundant or partial else
        'invalid' if status != 0 else
        'fully_constrained' if fully else 'under_constrained'
    )
    return {
        'object': sketch.Name, 'solver_status': status,
        'fully_constrained': fully, 'constraint_status': constraint_status,
        'degrees_of_freedom': int(sketch.DoF) if hasattr(sketch, 'DoF') else None,
        'redundant_constraint_numbers': redundant,
        'partially_redundant_constraint_numbers': partial,
        'conflicting_constraint_numbers': conflicting,
        'constraint_numbering': 'one_based',
        'native_status': str(getattr(sketch, 'getStatusString', lambda: '')()),
    }
