from types import SimpleNamespace

import pytest

from app.freecad.sketch_diagnostics import diagnose_sketch


@pytest.mark.parametrize('status,redundant,conflicting,dof,expected', [
    (-2, [3], [], 0, 'redundant'),
    (-4, [], [1, 3], -1, 'conflicting'),
    (-99, [], [], 0, 'invalid'),
    (0, [], [], 3, 'under_constrained'),
    (0, [], [], 0, 'fully_constrained'),
])
def test_diagnosis_uses_native_constraint_lists(status, redundant, conflicting, dof, expected):
    sketch = SimpleNamespace(Name='Profile', solve=lambda:status,
        RedundantConstraints=redundant, ConflictingConstraints=conflicting,
        PartiallyRedundantConstraints=[], DoF=dof, FullyConstrained=dof == 0,
        getStatusString=lambda:'native status')
    diagnosis=diagnose_sketch(sketch)
    assert diagnosis['constraint_status'] == expected
    assert diagnosis['redundant_constraint_numbers'] == redundant
    assert diagnosis['conflicting_constraint_numbers'] == conflicting
    assert diagnosis['degrees_of_freedom'] == dof
