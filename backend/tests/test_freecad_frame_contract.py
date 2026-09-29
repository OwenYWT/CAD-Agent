"""New placements have explicit semantics; retained plans keep their hashes."""
import pytest
from pydantic import ValidationError

from app.freecad.contracts import FreeCADOperation, SketchAddConstraintArgs


def test_explicit_body_frame_is_retained():
    frame = {"origin": {"x": 0, "y": 10, "z": 0},
             "normal": [0, -1, 0], "x_axis": [1, 0, 0]}
    op = FreeCADOperation(op_id="side", action="sketch.create", args={
        "name": "Side", "frame": frame})
    assert op.args["frame"]["origin"]["y"] == 10


def test_legacy_operation_canonical_form_has_no_new_defaults():
    op = FreeCADOperation(op_id="old", action="sketch.create", args={"name": "Old"})
    assert op.args == {"name": "Old", "body": "Body", "plane": "xy",
                       "offset_mm": 0.0, "reversed": False}


@pytest.mark.parametrize("value", [0, -23.5])
def test_coordinate_constraints_accept_signed_positions(value):
    arg = SketchAddConstraintArgs(sketch="Profile", kind="distance_x",
                                 first={"geometry_index": 0, "point_position": 1}, value_mm=value)
    assert arg.value_mm == value


@pytest.mark.parametrize("value", [0, -1])
def test_physical_lengths_still_require_positive_values(value):
    with pytest.raises(ValidationError):
        SketchAddConstraintArgs(sketch="Profile", kind="radius",
                                first={"geometry_index": 0}, value_mm=value)


@pytest.mark.parametrize("normal,x_axis", [([0, 0, 0], [1, 0, 0]),
                                           ([0, 0, 1], [1, 0, 1])])
def test_degenerate_or_nonorthogonal_frame_rejected(normal, x_axis):
    with pytest.raises(ValidationError):
        FreeCADOperation(op_id="side", action="sketch.create", args={
            "name": "Side", "frame": {"origin": {"x": 0, "y": 0, "z": 0},
                                        "normal": normal, "x_axis": x_axis}})


def test_sketch_angle_edit_has_explicit_degrees_not_millimetres():
    from app.freecad.contracts import SketchSetConstraintArgs
    import pytest
    from pydantic import ValidationError
    args={'sketch':'Arc','constraint_index':4,'expected_type':'Angle','value_deg':60}
    assert SketchSetConstraintArgs.model_validate(args).value_deg==60
    with pytest.raises(ValidationError):
        SketchSetConstraintArgs.model_validate({**args,'value_mm':60})
