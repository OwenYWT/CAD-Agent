"""Host, advertised schema and isolated runner must expose the same operations."""
import ast
import json
from pathlib import Path
from app.freecad.contracts import _ACTION_ARG_TYPES, capability_schemas
from app.freecad.operation_generator import _SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[2]

def test_advertised_capabilities_match_typed_contract_and_runner():
    manifest = json.loads((ROOT/'backend/app/freecad/capabilities.json').read_text())
    assert manifest['schema_version'] == 'freecad-capabilities.v1'
    expected = capability_schemas()
    assert manifest['actions'] == expected
    tree = ast.parse((ROOT/'backend/sandbox/freecad_entry.py').read_text())
    assignments = {node.targets[0].id: node.value for node in tree.body
        if isinstance(node,ast.Assign) and isinstance(node.targets[0],ast.Name)}
    assignments.update({node.target.id:node.value for node in tree.body if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name)})
    keys = ast.literal_eval(assignments['ACTION_KEYS'])
    assert set(keys) == set(expected) == {key.value for key in assignments['DISPATCH'].keys}
    for action,schema in expected.items():
        assert keys[action][0] == set(schema['properties'])
        assert set(schema.get('required',[])) <= keys[action][1]
        assert all('default' in schema['properties'][key] for key in keys[action][1]-set(schema.get('required',[])))
        assert '- '+action+':' in _SYSTEM_PROMPT


def test_geometry_discriminator_is_required_in_advertised_wire_schema():
    for action in ('sketch.add_geometry', 'sketch.add_profile'):
        schema = _ACTION_ARG_TYPES[action].model_json_schema()
        for reference in schema['properties']['geometry']['discriminator']['mapping'].values():
            variant = schema['$defs'][reference.rsplit('/', 1)[-1]]
            assert 'kind' in variant['required']


def test_planner_receives_schema_derived_tagged_shapes():
    from app.freecad.contracts import planner_discriminated_shapes
    shapes = planner_discriminated_shapes()
    assert shapes['sketch.add_geometry']['geometry']['tag'] == 'kind'
    assert shapes['sketch.add_geometry']['geometry']['variants']['line']['required'] == ['kind','start','end']
    assert shapes['feature.hole']['cut']['variants']['counterbore']['required'] == ['kind','diameter_mm','depth_mm']
    assert json.dumps(shapes, separators=(',', ':'), sort_keys=True) in _SYSTEM_PROMPT


def test_schema_identity_ignores_only_redundant_literal_enum():
    from app.freecad.contracts import normalize_capability_schema
    old = {'properties': {'kind': {'const': 'circle', 'enum': ['circle']},
                          'radius': {'exclusiveMinimum': 0}}}
    assert normalize_capability_schema(old) == {'properties': {
        'kind': {'const': 'circle'}, 'radius': {'exclusiveMinimum': 0}}}
    assert old['properties']['kind']['enum'] == ['circle']
    contradictory = {'const': 'circle', 'enum': ['rectangle']}
    assert normalize_capability_schema(contradictory) == contradictory
