"""Execution tool boundaries, using real schema/plan validation without a provider."""
import copy

import jsonschema
import pytest
from pydantic import ValidationError

from app.freecad.agent_tools import tool_schemas
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.selection import SelectionError, validate_selected_operations


def schema(name):
    return next(tool['function']['parameters'] for tool in tool_schemas(has_document=True)
                if tool['function']['name'] == name)


def program_request():
    return {
        'document_name': 'ParametricBox',
        'execution_mode': 'checkpoint',
        'execute': {'op_id': 'create-box', 'args': {
            'source': "box = document.addObject('Part::Box', 'Box')\nbox.Length = 17\nbox.Width = 13\nbox.Height = 7",
            'environment': 'headless', 'modules': ['Part']}},
        'export': {'op_id': 'save-box', 'args': {
            'objects': ['Box'], 'formats': ['fcstd'], 'basename': 'parametric_box'}},
    }


def persisted_plan(request):
    return {'document_name': request['document_name'], 'execution_mode': request['execution_mode'],
            'operations': [dict(request['execute'], action='api.execute'),
                           dict(request['export'], action='document.export')]}


@pytest.mark.parametrize('mixed', [False, True])
def test_typed_execution_schema_cannot_advertise_native_python(mixed):
    plan = persisted_plan(program_request())
    if mixed:
        plan['operations'].insert(1, {'op_id': 'sketch', 'action': 'sketch.create',
                                      'args': {'name': 'Sketch', 'body': 'Body'}})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, schema('freecad_execute'))


def test_native_tool_compiles_explicit_program_and_export_without_rewriting_source():
    from app.freecad.agent_tools import execution_plan

    request = program_request()
    original = copy.deepcopy(request)
    jsonschema.validate(request, schema('freecad_execute_api'))
    actual = execution_plan('freecad_execute_api', request)
    expected = FreeCADOperationPlan.model_validate(persisted_plan(request))
    assert actual.model_dump_json() == expected.model_dump_json()
    assert actual.operations[0].args['source'] == request['execute']['args']['source']
    assert request == original
    with pytest.raises(ValueError, match='freecad_execute_api'):
        execution_plan('freecad_execute', persisted_plan(request))


@pytest.mark.parametrize('invalid', ['missing_objects', 'empty_objects', 'injected_operation', 'authority'])
def test_native_tool_rejects_missing_selection_and_extra_operations(invalid):
    from app.freecad.agent_tools import execution_plan

    request = program_request()
    if invalid == 'missing_objects':
        del request['export']['args']['objects']
    elif invalid == 'empty_objects':
        request['export']['args']['objects'] = []
    elif invalid == 'injected_operation':
        request['operations'] = [{'op_id': 'injected', 'action': 'sketch.create', 'args': {'name': 'Sketch'}}]
    else:
        request['confirmed'] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(request, schema('freecad_execute_api'))
    with pytest.raises(ValidationError):
        execution_plan('freecad_execute_api', request)


def test_native_tool_keeps_runtime_identity_and_selection_guards():
    from app.freecad.agent_tools import execution_plan

    request = program_request()
    request['export']['op_id'] = request['execute']['op_id']
    with pytest.raises(ValidationError, match='unique'):
        execution_plan('freecad_execute_api', request)
    plan = execution_plan('freecad_execute_api', program_request())
    state = {'objects': [{'name': 'Box', 'type_id': 'Part::Box'}],
             'selection_context': {'features': [{'kernel_name': 'Box'}]}}
    with pytest.raises(SelectionError):
        validate_selected_operations(plan, state)


def test_typed_tool_keeps_original_runtime_plan_contract():
    from app.freecad.agent_tools import execution_plan

    plan = {'operations': [
        {'op_id': 'make-sketch', 'action': 'sketch.create', 'args': {'name': 'Sketch', 'body': 'CustomBody'}},
        {'op_id': 'save', 'action': 'document.export', 'args': {'formats': ['fcstd']}},
    ]}
    jsonschema.validate(plan, schema('freecad_execute'))
    assert execution_plan('freecad_execute', plan) == FreeCADOperationPlan.model_validate(plan)
    with pytest.raises(ValueError, match='unknown'):
        execution_plan('commit_revision', plan)
