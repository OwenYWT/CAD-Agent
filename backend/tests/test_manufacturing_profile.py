import pytest
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from app.agent.orchestrator import Orchestrator
from app.models.schemas import CADPlan, DesignBrief, GenerateResponse, ManufacturingProfile


@pytest.mark.asyncio
@pytest.mark.parametrize('process,material', [('fdm', 'PETG'), ('cnc', 'Aluminum')])
async def test_durable_intake_carries_saved_profile_without_adding_geometry_requirements(monkeypatch, process, material):
    from app.agent.durable_planner import DurableAgentPlanner
    from app.agent.planner import Planner
    from app.models.workflow_requests import McadAgentWorkflowV2Request
    from app.workflows.handlers import planning
    from tests.e2e.source_obligation_fixture import bind

    objective = '板40×30×12mm'
    original_contract = bind(objective)
    profile = ManufacturingProfile(process=process, material=material)
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
            content=json.dumps({'description': objective, 'part_type': 'plate', 'features': [],
                'dimensions': {'length': 40, 'width': 30, 'height': 12},
                'manufacturing_profile': {'process': 'sla', 'material': 'provider-invented'},
                'design_brief': {'acceptance': original_contract}})))])

    @asynccontextmanager
    async def transaction(*args, **kwargs):
        yield None

    async def stored(*args, **kwargs):
        return None

    async def start(*args, **kwargs):
        return uuid4()

    async def complete(*args, **kwargs):
        return kwargs['result']

    monkeypatch.setattr(planning, 'tenant_transaction', transaction)
    monkeypatch.setattr(planning, '_stored_agent_step_result', stored)
    monkeypatch.setattr(planning, '_start_agent_logical_step', start)
    monkeypatch.setattr(planning, '_complete_agent_logical_step', complete)
    monkeypatch.setattr(planning, '_fail_agent_logical_step', stored)
    planner = Planner()
    planner._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    request = McadAgentWorkflowV2Request(
        **{key: uuid4() for key in ('workflow_run_id', 'tenant_id', 'project_id', 'principal_id',
                                    'branch_id', 'expected_base_revision_id')},
        operation='generate', modeling_backend='auto', objective=objective,
        manufacturing_profile=profile.model_dump(mode='json'))
    result = await planning.agent_requirements({**request.temporal_payload(),
        'engineering_acceptance_v1': True, 'acceptance_bindings_v2': True},
        durable_planner=DurableAgentPlanner(planner=planner))
    assert len(calls) == 1
    messages = calls[0]['messages']
    assert any(profile.prompt_context() in message['content'] for message in messages)
    assert messages[-1]['content'] == objective
    requirements = result['requirements']
    assert requirements['manufacturing_profile'] == profile.model_dump(mode='json')
    assert process.upper() in requirements['design_brief']['manufacturing_posture']
    acceptance = requirements['design_brief']['acceptance']
    assert acceptance['objective'] == objective
    assert acceptance['checks'] == original_contract['checks']
    assert len(acceptance['checks']) == 3


def test_manufacturing_profile_schema_serializes_defaults():
    profile = ManufacturingProfile.fdm_pla_default()

    payload = profile.model_dump()

    assert payload["process"] == "fdm"
    assert payload["material"] == "PLA"
    assert payload["nozzle_diameter_mm"] == 0.4
    assert payload["layer_height_mm"] == 0.2
    assert payload["build_volume_mm"] == [220, 220, 250]


def test_generation_response_carries_manufacturing_profile():
    profile = ManufacturingProfile(process="sla", material="Resin")

    response = GenerateResponse(request_id="req-profile", success=True, manufacturing_profile=profile)

    assert response.model_dump()["manufacturing_profile"]["process"] == "sla"
    assert response.model_dump()["manufacturing_profile"]["material"] == "Resin"


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['generate', 'generate_2d', 'generate_assembly', 'modify', 'part', 'complex'])
async def test_source_generators_keep_manufacturing_context_in_provider_messages(path):
    from app.agent.code_gen import CodeGenerator
    from app.models.schemas import ModificationPlan
    from app.workflows.modeling import DurableModelingSourceGenerator
    profile = ManufacturingProfile(process='cnc', material='Aluminum')
    plan = CADPlan(description='plate', part_type='plate', dimensions={'length':40},
                   features=[], manufacturing_profile=profile)
    calls = []
    async def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content='result = existing_shape'))])
    generator = CodeGenerator()
    generator._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    if path == 'modify':
        await generator.modify(ModificationPlan(description='resize', modification_type='dimension_change'),
                               'result = existing_shape', [], [], manufacturing_profile=profile)
    elif path == 'part':
        await generator.generate_single_part('plate', plan.description, plan.dimensions, [], manufacturing_profile=profile)
    elif path == 'complex':
        await generator.generate_step('add base', '', 0, 1, DurableModelingSourceGenerator._plan_context(plan))
    else:
        await getattr(generator, path)(plan, [], [])
    assert len(calls) == 1
    assert any(profile.prompt_context() in message['content'] for message in calls[0]['messages'])


class RecordingPlanner:
    def __init__(self):
        self.messages = None

    async def plan_new(self, messages):
        self.messages = messages
        return CADPlan(
            description="Shelf bracket",
            part_type="bracket",
            dimensions={"wall_thickness": 3},
            features=["two holes"],
            design_brief=DesignBrief(
                intent_summary="Printable shelf bracket",
                artifact_type="bracket",
                open_questions=["Confirm load."],
            ),
        )


class FakeRetriever:
    async def find_similar(self, *args, **kwargs):
        return []


class RecordingCodeGenerator:
    async def generate(self, *args, **kwargs):
        return "result = cq.Workplane('XY').box(10, 10, 10)"


async def fake_execute_with_retry(self, request_id, code, plan, output_formats, on_step, prompt, is_2d=False):
    return GenerateResponse(
        request_id=request_id,
        success=True,
        code=code,
        files={"stl": f"/api/files/{request_id}/model.stl"},
        design_brief=plan.design_brief if plan else None,
    )


def build_test_orchestrator(planner: RecordingPlanner) -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.planner = planner
    orchestrator.code_gen = RecordingCodeGenerator()
    orchestrator.retriever = FakeRetriever()
    orchestrator.code_cache = None
    orchestrator._execute_with_retry = fake_execute_with_retry.__get__(orchestrator, Orchestrator)
    return orchestrator


@pytest.mark.asyncio
async def test_generate_injects_manufacturing_profile_before_planning():
    planner = RecordingPlanner()
    orchestrator = build_test_orchestrator(planner)
    profile = ManufacturingProfile(
        process="fdm",
        material="PETG",
        nozzle_diameter_mm=0.4,
        layer_height_mm=0.2,
        build_volume_mm=[220, 220, 250],
    )

    result = await orchestrator.generate("make a shelf bracket", manufacturing_profile=profile)

    prompt = planner.messages[0]["content"]
    assert "Manufacturing profile" in prompt
    assert "process=fdm" in prompt
    assert "material=PETG" in prompt
    assert result.success is True
    assert result.needs_confirmation is False
    assert result.manufacturing_profile == profile
    assert result.plan.manufacturing_profile == profile
    assert "FDM PETG" in result.design_brief.manufacturing_posture


@pytest.mark.asyncio
async def test_generation_brief_shows_manufacturing_profile_in_chinese():
    planner = RecordingPlanner()
    orchestrator = build_test_orchestrator(planner)
    profile = ManufacturingProfile(
        process="fdm",
        material="PETG",
        nozzle_diameter_mm=0.4,
        layer_height_mm=0.2,
        build_volume_mm=[220, 220, 250],
    )

    result = await orchestrator.generate("make a shelf bracket", manufacturing_profile=profile)

    targets = result.design_brief.printability_targets
    assert "\u5236\u9020\u5de5\u827a\uff1aFDM" in targets
    assert "\u6750\u6599\uff1aPETG" in targets
    assert "\u55b7\u5634\u76f4\u5f84\uff1a0.4 mm" in targets
    assert "\u5c42\u9ad8\uff1a0.2 mm" in targets
    assert "\u6210\u578b\u7a7a\u95f4\uff1a220 x 220 x 250 mm" in targets
    assert all("Manufacturing process:" not in item for item in targets)
    assert all("Nozzle diameter:" not in item for item in targets)
