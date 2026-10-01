import pytest
from pydantic import ValidationError

from app.contracts.acceptance import AcceptanceContract, MeasurementEvidence, acceptance_outcome


def contract(**change):
    data = {"objective": "一个整体零件", "checks": [{"check_id": "connected", "kind": "solid_count",
        "description": "One connected solid", "source_quote": "整体零件", "nominal": 1}]}
    data.update(change)
    return AcceptanceContract.model_validate(data)


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
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(payload,schema)
        if model is CADPlan:
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate({**payload,'acceptance':contract().model_dump(mode='json')},schema)
            jsonschema.validate({**payload,'part_type':'profile_2d'},schema)
            payload['design_brief']={'acceptance':contract().model_dump(mode='json')}
        else:
            payload['acceptance']=contract().model_dump(mode='json')
        jsonschema.validate(payload,schema)
