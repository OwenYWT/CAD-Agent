import pytest
from pydantic import ValidationError

from app.contracts.acceptance import AcceptanceContract, MeasurementEvidence, acceptance_outcome


def test_planar_gap_schema_and_runtime_reject_guessed_surface_points():
    import jsonschema
    from app.contracts.acceptance import AcceptanceCheck, acceptance_tool_schema
    raw = {'check_id': 'gap', 'kind': 'planar_gap', 'nominal': 25,
           'description': 'opening', 'source_quote': '25 mm gap'}
    schemas = [AcceptanceCheck.model_json_schema()]
    schema, definitions = acceptance_tool_schema()
    schemas.append({**schema, '$defs': definitions})
    assert AcceptanceCheck.model_validate(raw).kind == 'planar_gap'
    for schema in schemas:
        jsonschema.validate(raw, schema)
        for scope in ({'centers_mm': [[0, 0, 0]]}, {'point_mm': [0, 0, 0]}):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate({**raw, 'scope': scope}, schema)
            with pytest.raises(ValidationError):
                AcceptanceCheck.model_validate({**raw, 'scope': scope})


def contract(**change):
    data = {"objective": "一个整体零件", "checks": [{"check_id": "connected", "kind": "solid_count",
        "description": "One connected solid", "source_quote": "整体零件", "nominal": 1}]}
    data.update(change)
    return AcceptanceContract.model_validate(data)


def test_assembly_measurement_scope_frame_is_server_normalized_to_world():
    from app.agent.planner import _normalize_assembly_scope_frames

    payload = {
        "design_brief": {
            "acceptance": {
                "checks": [
                    {
                        "kind": "assembly_dimension",
                        "scope": {
                            "assembly_measurement": "groove_depth",
                            "frame": "assembly",
                        },
                    },
                    {
                        "kind": "overall_dimension",
                        "scope": {"frame": "invalid-relative-frame"},
                    },
                ]
            }
        }
    }

    _normalize_assembly_scope_frames(payload)

    checks = payload["design_brief"]["acceptance"]["checks"]
    assert checks[0]["scope"]["frame"] == "world"
    assert checks[1]["scope"]["frame"] == "invalid-relative-frame"


def test_missing_and_duplicate_evidence_never_pass():
    c = contract()
    good = MeasurementEvidence(check_id="connected", outcome="passed", method="brep_solids", measured=(1,))
    assert acceptance_outcome(c, ()) == "indeterminate"
    assert acceptance_outcome(c, (good, good)) == "indeterminate"
    assert acceptance_outcome(c, (good,)) == "passed"


def test_required_failure_and_unknown_are_distinct():
    c = contract()
    for outcome in ["failed", "indeterminate"]:
        evidence = MeasurementEvidence(check_id="connected", outcome=outcome, method="brep_solids")
        assert acceptance_outcome(c, (evidence,)) == outcome


def test_invented_source_and_promoted_assumptions_rejected():
    data = contract().model_dump(mode="json")
    data["checks"][0]["source_quote"] = "not in the user request"
    with pytest.raises(ValidationError, match="absent"):
        AcceptanceContract.model_validate(data)
    data["checks"][0]["source_kind"] = "assumption"
    with pytest.raises(ValidationError, match="unconfirmed"):
        AcceptanceContract.model_validate(data)


def test_provenance_defaults_do_not_promote_omitted_assumption_flag():
    import jsonschema
    from app.contracts.acceptance import AcceptanceCheck
    check=contract().model_dump(mode='json')['checks'][0]
    check.pop('required')
    for source_kind,expected in [('user',True),('confirmed',True),('assumption',False)]:
        item={**check,'source_kind':source_kind}
        jsonschema.validate(item,AcceptanceCheck.model_json_schema())
        assert AcceptanceCheck.model_validate(item).required is expected
    # An explicit contradictory flag remains an error; only omission defaults.
    for source_kind,required in [('assumption',True),('user',False),('confirmed',False)]:
        with pytest.raises(ValidationError):
            AcceptanceCheck.model_validate({**check,'source_kind':source_kind,'required':required})


def test_contract_identity_changes_when_target_or_scope_changes():
    c = contract()
    data = c.model_dump(mode="json")
    data["checks"][0]["nominal"] = 2
    assert AcceptanceContract.model_validate(data).digest() != c.digest()
    assert AcceptanceContract.model_validate(c.model_dump(mode="json")).digest() == c.digest()


def test_published_measurement_schema_and_runtime_agree_on_variant_rules():
    import jsonschema
    from app.contracts.acceptance import AcceptanceCheck
    schema=AcceptanceCheck.model_json_schema()
    position={'check_id':'position','kind':'hole_position','description':'centered hole',
              'source_quote':'centered','scope':{'axis':[0,0,1],'centers_mm':[[0,0,0]]}}
    jsonschema.validate(position,schema)
    assert AcceptanceCheck.model_validate(position).nominal is None
    invalid=[{**position,'nominal':0}, {**position,'scope':{}},
        {**position,'kind':'hole_depth'}, {**position,'kind':'solid_count','nominal':1.5},
        {**position,'kind':'overall_dimension','nominal':10,'scope':{}},
        {**position,'kind':'volume','nominal':100,'tolerance_mm':0.1}]
    for item in invalid:
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(item,schema)
        with pytest.raises(ValidationError):AcceptanceCheck.model_validate(item)


def test_unresolved_requirements_cannot_be_hidden_by_good_geometry():
    c = contract(unresolved=["Confirm whether two pieces are permitted"])
    e = MeasurementEvidence(check_id="connected", outcome="passed", method="brep_solids")
    assert acceptance_outcome(c, (e,)) == "indeterminate"


def test_verifier_limit_is_not_a_user_question_or_a_passing_contract():
    from app.agent.design_brief import ensure_design_brief
    from app.models.schemas import CADPlan, DesignBrief
    c=contract(verification_limits=['Required surface relationship has no implemented measurement'])
    plan=CADPlan(description='part',part_type='custom',dimensions={},features=[],
        design_brief=DesignBrief(acceptance=c))
    brief=ensure_design_brief(plan)
    assert not any('必须确认' in question for question in brief.open_questions)
    e=MeasurementEvidence(check_id='connected',outcome='passed',method='brep_solids')
    assert acceptance_outcome(c,(e,))=='indeterminate'
    assert 'verification_limits' not in contract().model_dump(mode='json')
    assert c.digest()!=contract().digest()


def test_surface_clearance_requires_exactly_two_explicit_face_identifiers():
    import jsonschema
    from app.contracts.acceptance import AcceptanceCheck
    data={'check_id':'clearance','kind':'surface_clearance','description':'minimum gap',
          'source_quote':'2 mm','nominal':2,'scope':{'centers_mm':[[0,10,0],[0,8,0]]}}
    schema=AcceptanceCheck.model_json_schema()
    jsonschema.validate(data,schema)
    AcceptanceCheck.model_validate(data)
    for count in (0,1,3):
        bad={**data,'scope':{'centers_mm':[[0,0,0]]*count}}
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(bad,schema)
        with pytest.raises(ValidationError):AcceptanceCheck.model_validate(bad)


def test_explicit_requirement_cannot_be_made_optional_to_pass():
    data=contract().model_dump(mode='json');data['checks'][0]['required']=False
    data['unresolved']=['another question']
    with pytest.raises(ValidationError,match='downgraded'):
        AcceptanceContract.model_validate(data)
    with pytest.raises(ValidationError,match='explicit criteria'):
        contract(checks=[])
    c=contract(checks=[],unresolved=['surface fit cannot yet be measured'])
    assert acceptance_outcome(c,())=='indeterminate'


@pytest.mark.parametrize('source_kind,required', [('user',False),('confirmed',False),('assumption',True)])
def test_tool_schema_exposes_provenance_requirements(source_kind,required):
    import jsonschema
    from app.contracts.acceptance import AcceptanceCheck
    check=contract().model_dump(mode='json')['checks'][0]
    check.update(source_kind=source_kind,required=required)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(check,AcceptanceCheck.model_json_schema())


@pytest.mark.asyncio
async def test_planner_repairs_against_actual_validation_feedback_without_weakening_contract():
    import json
    from types import SimpleNamespace
    from app.agent.planner import Planner
    payload={'description':'single part','modification_type':'dimension_change','target_params':{},
             'acceptance':contract().model_dump(mode='json')}
    bad=json.loads(json.dumps(payload));bad['acceptance']['checks'][0]['nominal']='unknown'
    calls=[]
    async def create(**kwargs):
        calls.append(kwargs)
        content=json.dumps(bad if len(calls)==1 else payload)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=content))])
    planner=Planner();planner._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result=await planner.plan_modification([{'role':'user','content':contract().objective}], '{}',require_acceptance=True)
    assert result.acceptance.digest()==contract().digest()
    assert len(calls)==2 and calls[0]['messages']!=calls[1]['messages']
    assert 'acceptance.checks.0.nominal' in calls[1]['messages'][-1]['content']
    assert 'do not remove requirements' in calls[1]['messages'][-1]['content']
    assert all('max_tokens' not in call for call in calls)


@pytest.mark.asyncio
async def test_new_plan_repairs_invalid_measurement_scope_frame():
    import json
    from types import SimpleNamespace
    from app.agent.planner import Planner

    objective = "创建一个整体零件"
    acceptance = {
        "objective": objective,
        "checks": [{
            "check_id": "connected",
            "kind": "solid_count",
            "description": "One connected solid",
            "source_quote": objective,
            "nominal": 1,
            "scope": {"frame": "world"},
        }],
    }
    invalid = {
        "description": objective,
        "part_type": "box",
        "dimensions": {"length": 10, "width": 10, "height": 10},
        "features": [],
        "constraints": [],
        "design_brief": {
            "intent_summary": objective,
            "artifact_type": "box",
            "acceptance": json.loads(json.dumps(acceptance)),
        },
    }
    invalid["design_brief"]["acceptance"]["checks"][0]["scope"]["frame"] = "assembly"
    corrected = json.loads(json.dumps(invalid))
    corrected["design_brief"]["acceptance"] = acceptance
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        payload = invalid if len(calls) == 1 else corrected
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content=json.dumps(payload)),
        )])

    planner = Planner()
    planner._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    result = await planner.plan_new(
        [{"role": "user", "content": objective}],
        require_acceptance=True,
    )

    assert result.design_brief.acceptance.checks[0].scope.frame == "world"
    assert len(calls) == 2
    feedback = calls[1]["messages"][-1]["content"]
    assert "design_brief.acceptance.checks.0.scope.frame" in feedback
    assert "world" in feedback and "bounds_center" in feedback
    assert "scope.frame must be exactly one of" in feedback


@pytest.mark.asyncio
async def test_v2_modification_repairs_illegal_bounding_box_for_ordinary_width_change():
    import json
    from types import SimpleNamespace
    from app.agent.planner import Planner

    objective = 'Change the width to 20 mm'
    valid_acceptance = {
        'schema_version': 'engineering-acceptance.v2',
        'objective': objective,
        'checks': [{
            'check_id': 'width',
            'kind': 'overall_dimension',
            'description': 'overall width',
            'source_quote': objective,
            'binding_ref': 'objective',
            'comparison': {'op': 'eq', 'value': 20.0, 'unit': 'mm'},
            'scope': {'axis': [0, 1, 0]},
        }],
    }
    invalid_acceptance = json.loads(json.dumps(valid_acceptance))
    invalid_acceptance['checks'][0].update({
        'check_id': 'bounds',
        'kind': 'bounding_box',
        'comparison': {'op': 'eq', 'value': 0.0, 'unit': 'mm'},
        'scope': {},
    })
    payloads = [
        {'description': 'part', 'modification_type': 'dimension_change',
         'target_params': {'width': '20 mm'}, 'acceptance': invalid_acceptance},
        {'description': 'part', 'modification_type': 'dimension_change',
         'target_params': {'width': '20 mm'}, 'acceptance': valid_acceptance},
    ]
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        content = json.dumps(payloads[len(calls) - 1])
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(content=content))])

    planner = Planner()
    planner._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    result = await planner.plan_modification(
        [{'role': 'user', 'content': objective}], '{}',
        require_acceptance=True, acceptance_version='engineering-acceptance.v2')

    assert result.acceptance.checks[0].kind == 'overall_dimension'
    assert len(calls) == 2
    system_prompt = calls[0]['messages'][0]['content']
    assert 'bounding_box' in system_prompt
    assert 'component_region' in system_prompt
    assert 'ordinary length, width, height' in system_prompt
    feedback = calls[1]['messages'][-1]['content']
    assert 'bounding_box' in feedback
    assert 'component_region' in feedback
    assert 'ordinary length, width, height' in feedback


@pytest.mark.asyncio
async def test_v1_modification_rejects_width_bound_to_the_pad_z_axis():
    import json
    from types import SimpleNamespace
    from app.agent.planner import Planner

    objective = '将正方体的宽改为 20cm，使其变成长方体'
    wrong = {
        'schema_version': 'engineering-acceptance.v1',
        'objective': objective,
        'checks': [{
            'check_id': 'width-z',
            'kind': 'overall_dimension',
            'description': '修改后的宽度',
            'source_quote': objective,
            'nominal': 200.0,
            'scope': {'axis': [0, 0, 1]},
        }],
    }
    corrected = json.loads(json.dumps(wrong))
    corrected['checks'][0].update(check_id='width-y', scope={'axis': [0, 1, 0]})
    payloads = [
        {'description': '修改正方体宽度', 'modification_type': 'dimension_change',
         'target_params': {'Pad.Length': '20 cm'}, 'acceptance': wrong},
        {'description': '修改正方体宽度', 'modification_type': 'dimension_change',
         'target_params': {'width': '20 cm'}, 'acceptance': corrected},
    ]
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(
                content=json.dumps(payloads[len(calls) - 1]))
        )])

    planner = Planner()
    planner._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    result = await planner.plan_modification(
        [{'role': 'user', 'content': objective}], '{}', require_acceptance=True)

    assert result.acceptance.checks[0].scope.axis == (0.0, 1.0, 0.0)
    assert len(calls) == 2
    assert 'width' in calls[1]['messages'][-1]['content'].lower()
    assert 'semantic' in calls[1]['messages'][-1]['content'].lower()


@pytest.mark.asyncio
async def test_failed_planner_preserves_field_diagnostic_without_echoing_input_value():
    import json
    from types import SimpleNamespace
    from app.agent.planner import Planner
    async def create(**kwargs):
        payload={'description':'part','modification_type':'dimension_change','target_params':{'width':'private-invalid-value'}}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=json.dumps(payload)))])
    planner=Planner();planner._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ValueError,match='target_params.width') as caught:
        await planner.plan_modification([{'role':'user','content':'width change'}], '{}')
    assert 'private-invalid-value' not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['generate', 'modify'])
async def test_engineering_requirements_use_typed_tool_and_preserve_repair_call_identity(operation):
    import copy
    import json
    from types import SimpleNamespace
    from app.agent.planner import Planner
    name = 'submit_cad_plan' if operation == 'generate' else 'submit_modification_plan'
    criteria = contract().model_dump(mode='json')
    payload = ({'description':'one part','part_type':'custom','dimensions':{},'features':[],
                'design_brief':{'acceptance':criteria}} if operation == 'generate' else
               {'description':'one part','modification_type':'dimension_change','target_params':{},
                'acceptance':criteria})
    bad = copy.deepcopy(payload)
    target = bad['design_brief']['acceptance'] if operation == 'generate' else bad['acceptance']
    target['checks'][0]['nominal'] = 'unknown'
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        assert kwargs['tools'][0]['function']['name'] == name
        # The old JSON-only example omits the engineering contract and conflicts
        # with the function definition. Neither generate nor modify may send it.
        assert '不要输出其他任何文字' not in kwargs['messages'][0]['content']
        import jsonschema
        schema=kwargs['tools'][0]['function']['parameters']
        jsonschema.validate(payload,schema)
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(bad,schema)
        assert 'response_format' not in kwargs and 'max_tokens' not in kwargs
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', message=SimpleNamespace(
            content=None, reasoning_content='retained-provider-context',tool_calls=[SimpleNamespace(
                id=f'plan-{len(calls)}', type='function', function=SimpleNamespace(
                    name=name, arguments=json.dumps(bad if len(calls)==1 else payload)))]))])
    planner=Planner();planner._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    messages=[{'role':'user','content':criteria['objective']}]
    result=(await planner.plan_new(messages, require_acceptance=True) if operation=='generate'
            else await planner.plan_modification(messages, '{}',require_acceptance=True))
    accepted=result.design_brief.acceptance if operation=='generate' else result.acceptance
    assert accepted.digest()==contract().digest()
    assert len(calls)==2
    assistant,feedback=calls[1]['messages'][-2:]
    assert assistant['reasoning_content']=='retained-provider-context'
    assert assistant['tool_calls'][0]['id']=='plan-1'
    assert feedback['role']=='tool' and feedback['tool_call_id']=='plan-1'
    assert 'nominal' in feedback['content'] and 'do not remove requirements' in feedback['content']


def test_engineering_tool_schema_requires_contract_at_the_actual_runtime_location():
    import jsonschema
    from app.agent.planner import _planning_format
    from app.models.schemas import CADPlan,ModificationPlan
    for model,name,payload in [(CADPlan,'submit_cad_plan',{'description':'part','part_type':'custom','dimensions':{},'features':[]}),
                               (ModificationPlan,'submit_modification_plan',{'description':'change','modification_type':'dimension_change'})]:
        schema=_planning_format(model,name,True)['tools'][0]['function']['parameters']
        for kind, wording in [('hole_position', 'matched one-to-one'),
                              ('surface_clearance', 'one on each face'),
                              ('void_connected', 'whose connectivity is tested')]:
            check_ref = schema['$defs']['AcceptanceCheck']['discriminator']['mapping'][kind].split('/')[-1]
            scope_ref = schema['$defs'][check_ref]['properties']['scope']['$ref'].split('/')[-1]
            scope = schema['$defs'][scope_ref]
            assert wording in scope['properties']['centers_mm'].get('description', '')
            assert 'centers_mm' in scope['required']
            if kind == 'hole_position':
                assert 'document axes' in scope['properties']['axis'].get('description', '')
                assert 'axis' in scope['required']
        if model is CADPlan:
            # Azure-compatible function schemas cannot express this conditional
            # requirement at the top level. Planner validation enforces it after
            # the provider returns the tool payload.
            jsonschema.validate(payload,schema)
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate({**payload,'acceptance':contract().model_dump(mode='json')},schema)
            jsonschema.validate({**payload,'part_type':'profile_2d'},schema)
            payload['design_brief']={'acceptance':contract().model_dump(mode='json')}
        else:
            with pytest.raises(jsonschema.ValidationError):jsonschema.validate(payload,schema)
            payload['acceptance']=contract().model_dump(mode='json')
        jsonschema.validate(payload,schema)


def test_generate_tool_schema_is_azure_function_compatible_at_the_top_level():
    from app.agent.planner import _planning_format
    from app.models.schemas import CADPlan

    schema = _planning_format(
        CADPlan, 'submit_cad_plan', True, 'engineering-acceptance.v2'
    )['tools'][0]['function']['parameters']

    assert schema['type'] == 'object'
    assert not any(key in schema for key in ('allOf', 'anyOf', 'oneOf', 'enum', 'const', 'not'))


def test_freecad_tool_schemas_are_azure_function_compatible_at_the_top_level():
    from app.freecad.agent_tools import tool_schemas

    configurations = (
        {'has_document': False},
        {'has_document': True},
        {'has_document': True, 'constraint_repair': True},
        {'has_document': True, 'profile_repair': True},
        {'has_document': True, 'cut_repair': True},
    )
    for configuration in configurations:
        for tool in tool_schemas(**configuration):
            schema = tool['function']['parameters']
            assert schema['type'] == 'object'
            assert not any(key in schema for key in ('allOf', 'anyOf', 'oneOf', 'enum', 'const', 'not'))


def test_v2_tool_schema_exposes_assembly_measurement_enum_and_units():
    import jsonschema
    from app.agent.planner import _planning_format
    from app.models.schemas import CADPlan
    from app.contracts.assembly_measurements import (
        ASSEMBLY_COUNTS, ASSEMBLY_DIMENSIONS, ASSEMBLY_PREDICATES,
    )

    schema = _planning_format(
        CADPlan, 'submit_cad_plan', True, 'engineering-acceptance.v2'
    )['tools'][0]['function']['parameters']
    definitions = schema['$defs']
    measurement = definitions['MeasurementScopeV2']['properties']['assembly_measurement']
    assert set(measurement['anyOf'][0]['enum']) == (
        set(ASSEMBLY_DIMENSIONS) | set(ASSEMBLY_COUNTS) | set(ASSEMBLY_PREDICATES)
    )
    assert any(
        rule.get('if', {}).get('properties', {}).get('kind', {}).get('const') == 'assembly_dimension'
        and rule.get('then', {}).get('properties', {}).get('comparison', {}).get('properties', {}).get('unit', {}).get('const') == 'mm'
        for rule in definitions['AcceptanceCheckV2'].get('allOf', [])
    )
    invalid = {
        'description': 'assembly', 'part_type': 'custom', 'dimensions': {}, 'features': [],
        'design_brief': {'acceptance': {
            'schema_version': 'engineering-acceptance.v2', 'objective': 'assembly',
            'checks': [{
                'check_id': 'bad', 'kind': 'assembly_dimension', 'description': 'bad',
                'source_quote': 'assembly', 'binding_ref': 'objective',
                'comparison': {'op': 'eq', 'value': 1.0, 'unit': 'count'},
                'scope': {'assembly_measurement': 'not_a_measurement'},
            }],
        }},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)


def test_v2_tool_schema_rejects_assembly_measurement_kind_mismatch():
    import jsonschema
    from app.agent.planner import _planning_format
    from app.models.schemas import CADPlan

    schema = _planning_format(
        CADPlan, 'submit_cad_plan', True, 'engineering-acceptance.v2'
    )['tools'][0]['function']['parameters']
    payload = {
        'description': 'assembly', 'part_type': 'custom', 'dimensions': {}, 'features': [],
        'design_brief': {'acceptance': {
            'schema_version': 'engineering-acceptance.v2', 'objective': 'assembly',
            'checks': [{
                'check_id': 'bad', 'kind': 'assembly_count', 'description': 'bad',
                'source_quote': 'assembly', 'binding_ref': 'objective',
                'comparison': {'op': 'eq', 'value': 90.0, 'unit': 'count'},
                'scope': {'assembly_measurement': 'envelope_length'},
            }],
        }},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_planner_correction_feedback_explains_assembly_measurement_roles():
    from app.agent.planner import _correction_messages

    feedback = _correction_messages(
        None,
        ValueError('assembly role does not define this measurement kind'),
    )
    message = feedback[-1]['content']
    assert 'assembly_dimension' in message
    assert 'envelope_length' in message
    assert 'assembly_count' in message
    assert 'tab_count' in message
    assert 'assembly_predicate' in message
    assert 'body_lid_fit' in message


def test_v2_tool_schema_rejects_local_selectors_on_assembly_measurements():
    import jsonschema
    from app.agent.planner import _planning_format
    from app.models.schemas import CADPlan

    schema = _planning_format(
        CADPlan, 'submit_cad_plan', True, 'engineering-acceptance.v2'
    )['tools'][0]['function']['parameters']
    payload = {
        'description': 'assembly', 'part_type': 'custom', 'dimensions': {}, 'features': [],
        'design_brief': {'acceptance': {
            'schema_version': 'engineering-acceptance.v2', 'objective': 'assembly',
            'checks': [{
                'check_id': 'bad', 'kind': 'assembly_dimension', 'description': 'bad',
                'source_quote': 'assembly', 'binding_ref': 'objective',
                'comparison': {'op': 'eq', 'value': 90.0, 'unit': 'mm'},
                'scope': {
                    'assembly_measurement': 'envelope_length',
                    'axis': [1.0, 0.0, 0.0],
                },
            }],
        }},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_planner_correction_feedback_explains_complete_assembly_scope():
    from app.agent.planner import _correction_messages

    feedback = _correction_messages(
        None,
        ValueError('assembly roles require the complete independently inventoried assembly'),
    )
    message = feedback[-1]['content']
    assert 'complete independently inventoried assembly' in message
    assert 'axis' in message
    assert 'point_mm' in message
    assert 'centers_mm' in message
    assert 'component_region' in message


def test_v2_tool_schema_rejects_hole_group_on_assembly_checks():
    import jsonschema
    from app.agent.planner import _planning_format
    from app.models.schemas import CADPlan

    schema = _planning_format(
        CADPlan, 'submit_cad_plan', True, 'engineering-acceptance.v2'
    )['tools'][0]['function']['parameters']
    payload = {
        'description': 'assembly', 'part_type': 'custom', 'dimensions': {}, 'features': [],
        'design_brief': {'acceptance': {
            'schema_version': 'engineering-acceptance.v2', 'objective': 'assembly',
            'checks': [{
                'check_id': 'bad', 'kind': 'assembly_count', 'description': 'bad',
                'source_quote': 'assembly', 'binding_ref': 'objective',
                'comparison': {'op': 'eq', 'value': 4.0, 'unit': 'count'},
                'scope': {'assembly_measurement': 'mounting_hole_count'},
                'hole_group': {'schema_version': 'hole-group.v1', 'group_id': 'mounting-holes'},
            }],
        }},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)


def test_planner_correction_feedback_explains_hole_group_authority():
    from app.agent.planner import _correction_messages

    feedback = _correction_messages(
        None,
        ValueError('unsupported hole population predicate or authority'),
    )
    message = feedback[-1]['content']
    assert 'hole_group' in message
    assert 'assembly_count' in message
    assert 'assembly_predicate' in message
    assert 'hole_count' in message


def test_v2_planner_prompt_declares_assembly_measurement_kind_mapping():
    from app.agent.planner import _v2_requirements_prompt

    prompt = _v2_requirements_prompt('design an enclosure assembly', 'submit_cad_plan')
    assert 'assembly_dimension' in prompt
    assert 'envelope_length' in prompt
    assert 'assembly_count' in prompt
    assert 'tab_count' in prompt
    assert 'assembly_predicate' in prompt
    assert 'body_lid_fit' in prompt
    assert "scope.frame must be exactly one of" in prompt
    assert "'world', 'bounds_center', 'bounds_min', 'selected_object'" in prompt
