from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.freecad.state_projector import project_object
from app.freecad.semantic_state import project_semantic_state


@pytest.mark.parametrize('type_id', ['App::Line', 'App::Plane'])
def test_native_reference_geometry_does_not_publish_physical_measurements(type_id):
    # A native reference shape must not even be measured as a finite solid.
    class ReferenceShape:
        def isNull(self): return False
        @property
        def Area(self): raise AssertionError('reference area has no physical meaning')
    obj = SimpleNamespace(Name='ArbitraryReference', TypeId=type_id, Shape=ReferenceShape())
    result = project_object(obj)
    assert result['reference_geometry']['extent'] == 'unbounded'
    assert 'shape' not in result
    assert result['inspection']['topology']['status'] == 'not_applicable'


def test_legacy_reference_projection_preserves_identity_placement_and_real_geometry():
    from app.freecad.reference_geometry import normalize_reference_state
    axis = {'name': 'Reference42', 'type_id': 'App::Line', 'out': ['Origin'],
            'global_placement': [1, 0, 0, 12], 'geometry_sha256': 'existing-brep-hash',
            'shape': {'area': 0., 'bounds_mm': {'max': [0, 10**100, 0]}},
            'inspection': {'topology': {'total': 1, 'items': [
                {'kind': 'edge', 'length_mm': 4 * 10**100, 'center': [0, 0, 0]}]}}}
    solid = {'name': 'Solid', 'type_id': 'PartDesign::Pad',
             'shape': {'area': 8e30, 'volume': 123.45678901234567},
             'properties': {'Length': '100 mm'}}
    state = {'schema_version': 'freecad-state.v2', 'objects': [axis, solid], 'parameters': []}
    original = deepcopy(state)
    normalized = normalize_reference_state(state)
    assert state == original
    assert normalized['objects'][1] == solid  # No arbitrary magnitude cutoff.
    ref = normalized['objects'][0]
    assert ref['global_placement'] == axis['global_placement']
    assert ref['out'] == axis['out'] and ref['name'] == axis['name']
    assert 'shape' not in ref
    assert ref['inspection']['topology']['items'] == []
    assert normalize_reference_state(normalized) == normalized
    document = uuid4()
    before = project_semantic_state(document, state)
    after = project_semantic_state(document, normalized)
    assert before['features'][0]['content_sha256'] == after['features'][0]['content_sha256']


def test_large_physical_geometry_is_not_treated_as_a_reference_by_name():
    from app.freecad.reference_geometry import normalize_reference_state
    state = {'schema_version': 'freecad-state.v2', 'objects': [
        {'name': 'Y_Axis', 'type_id': 'Part::Feature', 'shape': {'area': 4e100}}]}
    assert normalize_reference_state(state) == state


def test_old_checkpoint_inspection_reports_reference_semantics_not_huge_measurements():
    from app.freecad.inspection import InspectionRequest, inspect_state
    state = {'schema_version': 'freecad-state.v2', 'objects': [
        {'name': 'Reference', 'type_id': 'App::Line', 'inspection': {
            'topology': {'total': 1, 'items': [{'length_mm': 4 * 10**100}]}}}]}
    result = inspect_state(state, InspectionRequest(objects=['Reference'], fields=['topology']))
    item = result['objects'][0]
    assert item['reference_geometry']['extent'] == 'unbounded'
    assert item['topology']['status'] == 'not_applicable'
    assert item['topology']['items'] == []


def test_null_physical_shape_keeps_previous_projection_behavior():
    class NullShape:
        def isNull(self): return True
        def exportBrepToString(self): raise AssertionError('cannot export a null shape')
    result = project_object(SimpleNamespace(Name='EmptyFeature', TypeId='Part::Feature', Shape=NullShape()))
    assert 'shape' not in result
    assert 'geometry_sha256' not in result
