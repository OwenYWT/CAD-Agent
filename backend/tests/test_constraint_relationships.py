"""Exact relation proofs over frozen construction geometry, independent of the LLM."""
from fractions import Fraction

from app.freecad.constraint_relationships import relation_rows, derive_row, satisfies


def test_closed_chain_total_is_a_derived_expression_not_an_independent_driver():
    geometry = [
        {'index': 0, 'type': 'Part::GeomLineSegment', 'start': [0, 0, 0], 'end': [0, 5, 0]},
        {'index': 1, 'type': 'Part::GeomLineSegment', 'start': [0, 5, 0], 'end': [0, 30, 0]},
        {'index': 2, 'type': 'Part::GeomLineSegment', 'start': [0, 30, 0], 'end': [0, 35, 0]},
        {'index': 3, 'type': 'Part::GeomLineSegment', 'start': [0, 0, 0], 'end': [0, 35, 0]},
    ]
    def constraint(kind, first, point=None, second=None, end=None, value=None):
        return {'kind': kind, 'first': {'geometry_index': first, 'point_position': point},
            'second': {'geometry_index': second, 'point_position': end} if second is not None else None,
            'value_mm': value}
    basis = []
    for index, value in enumerate((5, 25, 5)):
        basis += relation_rows(constraint('distance', index, value=value), geometry)
    for first, a, second, b in ((0, 2, 1, 1), (1, 2, 2, 1), (0, 1, 3, 1), (2, 2, 3, 2)):
        basis += relation_rows(constraint('coincident', first, a, second, b), geometry)
    target = relation_rows(constraint('distance', 3, value=35), geometry)[0]
    proof = derive_row(target, basis)
    assert proof is not None
    assert proof[:3] == [Fraction(1), Fraction(1), Fraction(1)]
    assert satisfies(target, geometry)
    # Changing only the proposed total cannot acquire a false equivalence proof.
    assert derive_row(relation_rows(constraint('distance', 3, value=36), geometry)[0], basis) is None


def test_unknown_and_nonlinear_relationships_are_not_assumed_safe():
    geometry = [{'index': 0, 'type': 'Part::GeomLineSegment', 'start': [0, 0, 0], 'end': [3, 4, 0]}]
    assert relation_rows({'kind': 'distance', 'first': {'geometry_index': 0}, 'value_mm': 5}, geometry) is None


def test_coincidence_proof_checks_both_coordinates_not_just_equal_lengths():
    geometry = [
        {'index': 0, 'type': 'Part::GeomLineSegment', 'start': [0, 0, 0], 'end': [4, 0, 0]},
        {'index': 1, 'type': 'Part::GeomLineSegment', 'start': [0, 4, 0], 'end': [4, 4, 0]},
    ]
    rows = relation_rows({'kind': 'coincident', 'first': {'geometry_index': 0, 'point_position': 2},
        'second': {'geometry_index': 1, 'point_position': 2}}, geometry)
    assert not all(satisfies(row, geometry) for row in rows)
def test_origin_datum_proves_coordinates_without_an_editable_zero_dimension():
    from app.freecad.contracts import SketchAddConstraintArgs
    from app.freecad.constraint_relationships import relation_rows, derive_row
    geometry = [{'index': 0, 'type': 'Part::GeomLineSegment', 'start': [0, 0, 0], 'end': [12, 0, 0]}]
    args = SketchAddConstraintArgs.model_validate({'sketch': 'Sketch', 'kind': 'coincident',
        'first': {'geometry_index': 0, 'point_position': 1}, 'second': {'datum': 'origin'}})
    rows = relation_rows(args.model_dump(), geometry)
    assert len(rows) == 2
    for kind in ('distance_x', 'distance_y'):
        old = relation_rows({'kind': kind, 'first': {'geometry_index': 0, 'point_position': 1}, 'value_mm': 0}, geometry)[0]
        assert derive_row(old, rows) is not None
