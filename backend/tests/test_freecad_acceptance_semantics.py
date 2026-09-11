"""Counterexamples from independent acceptance; compiler may safely decline."""
import pytest

from app.freecad.operation_compiler import compile_common_generation


@pytest.mark.parametrize("features,constraints", [
    (["hole:count=4,diameter=6,through_all,positions=four_corners"],
     ["each hole center is 10mm from adjacent edges", "no hole at center"]),
    (["hole:diameter=6,depth=3,blind,center=(20,20)"],
     ["must not break through 8mm plate"]),
    (["hole:diameter=6,center=(20,20)", "hole:diameter=10,center=(70,50)"],
     ["exactly two holes"]),
    (["through_hole:diameter=6,count=1,position=centered"],
     ["add an engraved serial number on the top face"]),
])
def test_compiler_declines_semantics_outside_its_complete_grammar(features, constraints):
    requirements = {
        "part_type": "plate",
        "dimensions": {"length": 80, "width": 60, "thickness": 8, "hole_diameter": 6},
        "features": features, "constraints": constraints,
    }
    assert compile_common_generation(requirements, output_formats=("step", "stl")) is None


def test_compiler_does_not_turn_a_bracket_into_a_plain_box():
    assert compile_common_generation({
        "part_type": "bracket", "dimensions": {"width": 80, "depth": 60, "height": 8},
        "features": [], "constraints": [],
    }, output_formats=("step",)) is None


def test_existing_singular_centered_hole_grammar_remains_supported():
    # Actual persisted output of the existing requirements planner. A singular
    # centered-hole descriptor has one position; an explicit non-one count or
    # any pattern/offset/depth must still leave this compiler.
    result = compile_common_generation({
        "part_type": "plate", "dimensions": {"width": 60, "depth": 40, "thickness": 8},
        "features": ["through_hole:diameter=6,position=centered"],
        "constraints": ["sketch_fully_constrained=true", "hole_centered=true"],
    }, output_formats=("step", "stl"))
    assert result is not None
    assert sum(op.action == "feature.hole" for op in result.operations) == 1


def test_exact_negative_edge_constraint_is_consumed_not_dropped():
    request = {"part_type": "plate", "dimensions": {"length": 60, "width": 40, "thickness": 8},
        "features": ["through_hole:diameter=6,count=1,position=centered"],
        "constraints": ["hole_position=centered", "no_unrequested_fillets_or_chamfers=true"]}
    assert compile_common_generation(request, output_formats=("step",)) is not None
    request["features"].append("fillet:radius=1,edges=all_outer")
    assert compile_common_generation(request, output_formats=("step",)) is None
