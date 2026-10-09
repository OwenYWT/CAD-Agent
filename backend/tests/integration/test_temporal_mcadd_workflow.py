"""Real Temporal + PostgreSQL + MinIO + Podman MCAD workflow tests."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import signal
import socket
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from temporalio.client import WorkflowFailureError

from app.config import settings
from app.agent.assembly_planner import AssemblyPart, AssemblyPlan
from app.agent.durable_planner import DurableAgentPlanner
from app.agent.durable_repair import DurableRepairResult
from app.validation.durable_visual import DurableVisualReport, VisualJudgment
from app.agent.multi_step import BuildPhase, BuildPlan, BuildStep
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.execution.composition import get_execution_backend
from app.execution.capability_adapter import CapabilityExecutionAdapter
from app.execution.contracts import ExecutionStatus
from app.object_store import delete_object, get_object, reset_object_store_client
from app.services.event_relay import (
    get_change_set_detail,
    get_task_snapshot,
    read_task_events,
)
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.repositories.revisions import (
    compare_and_swap_branch_head,
    create_candidate_change_set,
    create_initial_branch,
)
from app.services.change_sets import accept_change_set, commit_change_set
from app.temporal_client import get_temporal_client, reset_temporal_client
from app.workers.workflow_worker import (
    build_agent_v2_workflow_worker,
    build_workflow_worker,
)
from app.workflows.modeling import SourceGenerationResult
from app.workflows.modeling import DurableModelingSourceGenerator
from app.workflows.temporal import (
    McadAgentWorkflowV2Request,
    McadExecutionRequest,
    cancel_mcad_workflow,
    confirm_mcad_workflow,
    start_mcad_workflow,
    temporal_agent_v2_workflow_id,
)


REAL_FREECAD_AGENT = os.environ.get("CAD_AGENT_TEST_REAL_FREECAD_AGENT") == "1"


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
RUN_EXTERNAL = (
    os.environ.get("CAD_AGENT_TEST_OBJECT_STORE") == "1"
    and os.environ.get("CAD_AGENT_TEST_TEMPORAL") == "1"
    and bool(os.environ.get("TEMPORAL_TARGET"))
)
pytestmark = [
    pytest.mark.skipif(
        not TEST_DATABASE_URL or not RUN_EXTERNAL,
        reason="real PostgreSQL, MinIO, Temporal, and Podman are required",
    ),
    pytest.mark.slow,
]


@pytest.fixture(autouse=True)
def provider_credentials_are_scoped_to_live_cases(request, monkeypatch):
    live = {
        "test_agent_v2_real_freecad_generation_validation_seal_and_commit",
        "test_agent_v2_real_visual_provider_persists_provenance",
        "test_agent_v2_real_repair_provider_persists_provenance_and_attempt",
        "test_agent_v2_real_planner_retriever_codegen_and_execution_provenance",
        "test_agent_v2_live_freecad_tools_contract",
        "test_agent_v2_live_freecad_checkpoint_contract",
    }
    if REAL_FREECAD_AGENT and os.environ.get('CAD_CONSTRAINT_LIVE_REPAIR') == '1':
        live.add('test_constraint_patch_incident_full_chain_and_owned_evidence')
    if request.node.originalname not in live:
        # The same invocation may include live-provider and controlled cases.
        # Credentials must not silently change assertions in controlled cases.
        for name in ("moonshot_api_key", "dashscope_api_key", "azure_openai_api_key"):
            monkeypatch.setattr(settings, name, None)


@pytest.fixture(autouse=True)
def legacy_cadquery_history(request, monkeypatch):
    """Exercise the retained pre-cutover source-code history explicitly.

    These fixtures inject CadQuery codegen/repair providers. New 3D generation
    intentionally selects FreeCAD, so running them as a new policy history no
    longer reaches their fault-injection boundary. Only the old Temporal patch
    decision is controlled; execution, fencing, artifacts and recovery stay real.
    New-policy FreeCAD is covered by the fused test and live HTTP/browser suite.
    """
    source_history_tests = {
        "test_agent_v2_confirmed_plan_seals_reviewable_candidate",
        "test_agent_v2_real_visual_provider_persists_provenance",
        "test_agent_v2_visual_mismatch_repairs_and_revalidates_geometry",
        "test_agent_v2_user_code_failure_creates_durable_repair_attempt",
        "test_agent_v2_geometry_failure_repairs_and_revalidates_new_manifest",
        "test_agent_v2_repeated_repair_failure_stops_without_second_llm_call",
        "test_agent_v2_complex_steps_survive_worker_restart_without_regeneration",
        "test_agent_v2_real_repair_provider_persists_provenance_and_attempt",
        "test_agent_v2_real_planner_retriever_codegen_and_execution_provenance",
    }
    legacy_validation_tests = source_history_tests | {
        "test_agent_v2_assembly_executes_parts_then_combine_with_source_edges",
    }
    legacy_requirement_tests = legacy_validation_tests | {
        "test_agent_v2_plan_waits_before_candidate_source_or_execution",
        "test_agent_v2_assembly_partial_failure_preserves_successful_part",
        "test_agent_v2_assembly_cancel_stops_active_parts_before_execution",
    }
    if request.node.name not in legacy_requirement_tests:
        return
    from temporalio import workflow

    original = workflow.patched

    def legacy_patch(change_id):
        if change_id in {'agent-v2-engineering-acceptance-v1','agent-v2-final-solid-acceptance-v1'}:
            return False
        # Retain one genuine pre-model-job command history for the replay gate.
        # Other V2 cases keep the new child-workflow path enabled.
        if change_id == "agent-v2-model-jobs-v1" and request.node.name == "test_agent_v2_confirmed_plan_seals_reviewable_candidate":
            return False
        if change_id == "agent-v2-backend-policy-v1" and request.node.name in source_history_tests:
            return False
        if change_id == "agent-v2-validation-repair-v2" and request.node.name in legacy_validation_tests:
            return False
        return original(change_id)

    monkeypatch.setattr(workflow, "patched", legacy_patch)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL or not RUN_EXTERNAL:
        yield
        return
    settings.database_url = TEST_DATABASE_URL
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def external_lifecycle(migrated_database):
    if not TEST_DATABASE_URL or not RUN_EXTERNAL:
        yield
        return
    original = {
        "database_url": settings.database_url,
        "temporal_target": settings.temporal_target,
        "sandbox_runtime": settings.sandbox_runtime,
        "sandbox_command": settings.sandbox_command,
        "sandbox_image": settings.sandbox_image,
        "moonshot_api_key": settings.moonshot_api_key,
        "dashscope_api_key": settings.dashscope_api_key,
        "azure_openai_api_key": settings.azure_openai_api_key,
        "vision_model": settings.vision_model,
    }
    settings.database_url = TEST_DATABASE_URL
    settings.temporal_target = os.environ["TEMPORAL_TARGET"]
    settings.sandbox_runtime = os.environ.get("SANDBOX_RUNTIME", "podman")
    settings.sandbox_command = os.environ.get("SANDBOX_COMMAND", "podman")
    settings.sandbox_image = os.environ.get(
        "SANDBOX_IMAGE",
        "localhost/cad-agent-sandbox:m0-unified",
    )
    real_provider = (
        REAL_FREECAD_AGENT
        or os.environ.get("CAD_AGENT_TEST_REAL_LLM") == "1"
        or os.environ.get("CAD_AGENT_TEST_REAL_VISION") == "1"
    )
    if real_provider:
        settings.moonshot_api_key = os.environ.get(
            "MOONSHOT_API_KEY",
            settings.moonshot_api_key,
        )
        settings.vision_model = os.environ.get(
            "VISION_MODEL",
            settings.vision_model,
        )
    else:
        # These tests intentionally cover durable orchestration without external
        # provider calls. Keep them hermetic regardless of the pytest cwd or a
        # developer's local backend/.env.
        settings.moonshot_api_key = None
        settings.dashscope_api_key = None
        settings.azure_openai_api_key = None
    reset_temporal_client()
    reset_object_store_client()
    get_execution_backend.cache_clear()
    yield
    await close_database()
    reset_temporal_client()
    reset_object_store_client()
    get_execution_backend.cache_clear()
    for key, value in original.items():
        setattr(settings, key, value)


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_control_plane():
    if not TEST_DATABASE_URL or not RUN_EXTERNAL:
        yield
        return
    async def clean() -> None:
        async with get_database_engine().begin() as connection:
            object_keys = list(
                (
                    await connection.execute(
                        text(
                            "SELECT object_key FROM artifacts "
                            "UNION SELECT staging_object_key FROM artifact_uploads"
                        )
                    )
                ).scalars()
            )
            staging_keys = list(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT output->>'object_key'
                            FROM agent_staging_manifests,
                                 jsonb_array_elements(COALESCE(manifest->'outputs','[]'::jsonb)
                                     || COALESCE(manifest->'verification_outputs','[]'::jsonb)) output
                            """
                        )
                    )
                ).scalars()
            )
            evidence_keys = list(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT render->>'object_key'
                            FROM agent_validation_evidence
                            CROSS JOIN LATERAL jsonb_array_elements(
                                COALESCE(evidence->'renders', '[]'::jsonb)
                            ) render
                            UNION
                            SELECT evidence->'report_artifact'->>'object_key'
                            FROM agent_validation_evidence
                            WHERE evidence ? 'report_artifact'
                            """
                        )
                    )
                ).scalars()
            )
        for key in [*object_keys, *staging_keys, *evidence_keys]:
            if key:
                await delete_object(key)
        async with get_database_engine().begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE artifacts, artifact_uploads, change_sets, "
                    "project_revisions, project_branches, outbox_messages, "
                    "task_events, execution_attempts, step_runs, workflow_runs, "
                    "usage_meter_entries, audit_records, project_memberships, "
                    "projects, tenant_memberships, principals, tenants CASCADE"
                )
            )

    # A failed/timed-out test may leave a Temporal history retrying after its
    # relational fixture is cleared. Never let the next test consume that work.
    queues = (settings.temporal_task_queue, settings.temporal_agent_v2_task_queue)
    suffix = uuid4().hex[:12]
    settings.temporal_task_queue = f"{queues[0]}-{suffix}"
    settings.temporal_agent_v2_task_queue = f"{queues[1]}-{suffix}"
    await clean()
    try:
        yield
    finally:
        if os.environ.get('CAD_AGENT_TEST_EVIDENCE_DIR'):
            from pathlib import Path
            retained=Path(os.environ['CAD_AGENT_TEST_EVIDENCE_DIR'])/('run-'+suffix)
            retained.mkdir(parents=True,exist_ok=True)
            async with get_database_engine().connect() as connection:
                diagnostics={}
                for table in ('workflow_runs','step_runs','agent_validation_evidence','llm_calls'):
                    diagnostics[table]=[dict(row) for row in (await connection.execute(text('SELECT * FROM '+table))).mappings()]
                artifacts=[dict(row) for row in (await connection.execute(text('SELECT artifact_kind,object_key,sha256 FROM artifacts'))).mappings()]
            (retained/'diagnostics.json').write_text(json.dumps(diagnostics,ensure_ascii=False,default=str,indent=2))
            for artifact in artifacts:
                if artifact['artifact_kind'] in {'fcstd','step','state'}:
                    (retained/(artifact['sha256']+'.'+artifact['artifact_kind'])).write_bytes(await get_object(artifact['object_key']))
        await clean()
        settings.temporal_task_queue, settings.temporal_agent_v2_task_queue = queues


async def _seed_project(label: str):
    owner = user_principal(f"temporal-{label}-{uuid4()}")
    project_id = uuid4()
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name=f"Temporal {label}",
            slug=f"temporal-{project_id.hex[:12]}",
        )
        initial = await create_initial_branch(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            created_by_principal_id=owner.principal_id,
            branch_name="main",
            initial_manifest={"state": "empty"},
        )
    return owner, project_id, initial


async def _wait_for_status(owner, workflow_id, expected: set[str], timeout=30):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            status = await connection.scalar(
                text("SELECT status FROM workflow_runs WHERE id=:id"),
                {"id": workflow_id},
            )
        if status in expected:
            return status
        if status in {"failed", "cancelled", "timed_out", "succeeded"}:
            async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
                diagnostic = (await connection.execute(text(
                    "SELECT status,error_code,error_message FROM workflow_runs WHERE id=:id"
                ), {"id": workflow_id})).mappings().one()
            raise AssertionError(f"Unexpected terminal workflow: {dict(diagnostic)}")
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"workflow {workflow_id} did not reach {sorted(expected)}"
    )


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.skipif(
    not REAL_FREECAD_AGENT,
    reason="set CAD_AGENT_TEST_REAL_FREECAD_AGENT=1 for the real fused flow",
)
@pytest.mark.parametrize("scope_instruction", [
    "Add a 1 mm chamfer to all outer edges. Do not chamfer the hole mouths.",
    "仅外边倒角1mm，孔口保持不变。",
], ids=["en-protected-mouths", "zh-protected-mouths"])
async def test_agent_v2_real_freecad_generation_validation_seal_and_commit(scope_instruction):
    owner, project_id, initial = await _seed_project("agent-v2-freecad-real")
    client = await get_temporal_client()
    objective = (
        "Create one rectangular plate 100 mm long, 60 mm wide and 10 mm thick. "
        "Add one centered 6 mm diameter through hole. Export STEP and STL."
    )
    request_payload = {
        "branch_id": str(initial.branch_id),
        "expected_base_revision_id": str(initial.revision_id),
        "operation": "generate",
        "modeling_backend": "freecad",
        "objective": objective,
    }
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-freecad-real-{project_id}",
            request_payload=request_payload,
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        modeling_backend="freecad",
        objective=objective,
        output_formats=("step", "stl"),
        confirmation_timeout_seconds=180,
    )

    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        for _ in range(1200):
            phase = await handle.query("phase")
            if phase == "waiting_confirmation":
                await confirm_mcad_workflow(
                    created.workflow_id,
                    accepted=True,
                    note="确认真实 FreeCAD 计划",
                    workflow_kind="mcad.agent.v2.generate",
                )
                break
            if phase.startswith("executing:") or phase.startswith("validating:"):
                break
            if phase in {"failed", "cancelled", "timed_out", "reviewable"}:
                break
            await asyncio.sleep(0.1)
        try:
            result = await asyncio.wait_for(handle.result(), timeout=300)
        except Exception as exc:
            diagnostic = await _snapshot(owner, created.workflow_id)
            raise AssertionError(
                json.dumps(diagnostic, ensure_ascii=False, default=str)
            ) from exc

    assert result["status"] == "succeeded"
    assert result["candidate_revision_id"]
    assert result["change_set_id"]
    artifacts = {item["artifact_kind"]: item for item in result["artifacts"]}
    assert {"fcstd", "state", "step", "stl"}.issubset(artifacts)
    for kind in ("fcstd", "state", "step", "stl"):
        payload = await get_object(artifacts[kind]["object_key"])
        assert len(payload) == artifacts[kind]["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == artifacts[kind]["sha256"]

    change_set_id = UUID(result["change_set_id"])
    accepted = await accept_change_set(
        tenant_id=owner.tenant_id,
        reviewer_principal_id=owner.principal_id,
        change_set_id=change_set_id,
        review_note="真实 FreeCAD 回归通过",
    )
    assert accepted.status == "accepted"
    committed = await commit_change_set(
        tenant_id=owner.tenant_id,
        reviewer_principal_id=owner.principal_id,
        change_set_id=change_set_id,
    )
    assert committed.status == "committed"
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        head = await connection.scalar(
            text("SELECT head_revision_id FROM project_branches WHERE id=:id"),
            {"id": initial.branch_id},
        )
    assert str(head) == result["candidate_revision_id"]

    generated_revision_id = UUID(result["candidate_revision_id"])
    from app.services.cloud_documents import document_snapshot, document_events, DocumentConflict
    from app.services.feature_annotations import save_annotation
    semantic = await document_snapshot(owner, initial.branch_id)
    # Both a native Hole and a circular through-all Pocket represent the user's
    # requested geometry. Names and that modeling choice are not requirements.
    holes = [f for f in semantic['features'] if f['type'] in {'PartDesign::Hole', 'PartDesign::Pocket'}]
    assert len(holes) == 1, [(f['type'], f['kernel_name']) for f in semantic['features']]
    hole_feature = holes[0]
    def assert_centered_cut(feature, radius):
        shape = feature['shape']
        low, high = shape['bounds_mm']['min'], shape['bounds_mm']['max']
        assert [b-a for a,b in zip(low, high)] == pytest.approx([100,60,10])
        assert shape['volume'] == pytest.approx(100*60*10 - math.pi*radius**2*10)
        circular = [b for b in feature['topology_bindings'] if b['geometry'] == 'circular']
        assert len(circular) == 2
        for binding in circular:
            assert binding['radius_mm'] == pytest.approx(radius)
            assert [binding['center']['x']-low[0], binding['center']['y']-low[1]] == pytest.approx([50,30])
        assert sorted(b['center']['z'] for b in circular) == pytest.approx([low[2], high[2]])
    assert_centered_cut(hole_feature, 3)
    annotation = dict(revision_id=generated_revision_id, expected_version=0,
        annotation_id=uuid4(), role='定位孔', intent='保持同心，适配定位销直径')
    saved = await save_annotation(owner,initial.branch_id,UUID(hole_feature['id']),**annotation)
    assert saved == {'version':1,'replayed':False}
    assert (await save_annotation(owner,initial.branch_id,UUID(hole_feature['id']),**annotation))['replayed']
    with pytest.raises(DocumentConflict):
        await save_annotation(owner,initial.branch_id,UUID(hole_feature['id']),
            **{**annotation,'annotation_id':uuid4(),'intent':'stale edit'})
    annotated = await document_snapshot(owner,initial.branch_id)
    assert annotated['state_version']==semantic['state_version']
    assert next(f for f in annotated['features'] if f['id']==hole_feature['id'])['role']=='定位孔'
    events=await document_events(owner,initial.branch_id,semantic['event_sequence'])
    assert events[-1]['event_type']=='feature.annotated'
    modify_objective = (
        "Change the existing centered through hole diameter from 6 mm to 8 mm "
        f"and preserve all other dimensions. {scope_instruction} Export STEP and STL."
    )
    modify_payload = {
        "branch_id": str(initial.branch_id),
        "expected_base_revision_id": str(generated_revision_id),
        "operation": "modify",
        "modeling_backend": "freecad",
        "objective": modify_objective,
    }
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        modified_run = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.modify",
            idempotency_key=f"agent-v2-freecad-modify-real-{project_id}",
            request_payload=modify_payload,
        )
    await save_annotation(owner,initial.branch_id,UUID(hole_feature['id']),
        **{**annotation,'annotation_id':uuid4(),'expected_version':1,'intent':'后续任务需重新确认销径'})
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as connection:
        frozen = await connection.scalar(text("SELECT arguments->'_feature_annotations' FROM cad_operations WHERE id=:id"),{'id':modified_run.workflow_id})
        assert frozen[0]['version']==1 and frozen[0]['intent']==annotation['intent']
    modify_request = McadAgentWorkflowV2Request(
        workflow_run_id=modified_run.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=generated_revision_id,
        operation="modify",
        modeling_backend="freecad",
        objective=modify_objective,
        output_formats=("step", "stl"),
        confirmation_timeout_seconds=180,
    )

    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
    ):
        modify_handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            modify_request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(modified_run.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        for _ in range(1200):
            phase = await modify_handle.query("phase")
            if phase == "waiting_confirmation":
                await confirm_mcad_workflow(
                    modified_run.workflow_id,
                    accepted=True,
                    note="确认真实 FreeCAD 参数修改",
                    workflow_kind="mcad.agent.v2.modify",
                )
                break
            if phase.startswith("executing:") or phase.startswith("validating:"):
                break
            if phase in {"failed", "cancelled", "timed_out", "reviewable"}:
                break
            await asyncio.sleep(0.1)
        try:
            modify_result = await asyncio.wait_for(
                modify_handle.result(),
                timeout=300,
            )
        except Exception as exc:
            diagnostic = await _snapshot(owner, modified_run.workflow_id)
            raise AssertionError(
                json.dumps(diagnostic, ensure_ascii=False, default=str)
            ) from exc

    assert modify_result["status"] == "succeeded"
    modified_artifacts = {
        item["artifact_kind"]: item for item in modify_result["artifacts"]
    }
    assert {"fcstd", "state", "step", "stl"}.issubset(modified_artifacts)
    modified_state_bytes = await get_object(modified_artifacts["state"]["object_key"])
    modified_state = json.loads(modified_state_bytes)
    objects = {item["name"]: item for item in modified_state["objects"]}
    assert hole_feature['kernel_name'] in objects
    chamfers = [obj for obj in objects.values() if obj['type_id'] == 'PartDesign::Chamfer']
    assert len(chamfers) == 1 and chamfers[0]['properties']['Size'].startswith('1.00 mm')

    # Verify final artifacts, not the upstream Hole feature hidden by Chamfer.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix='chamfer-final-') as directory:
        Path(directory).chmod(0o755)
        for kind, filename in [('fcstd','model.FCStd'),('step','model.step')]:
            payload = await get_object(modified_artifacts[kind]['object_key'])
            assert hashlib.sha256(payload).hexdigest() == modified_artifacts[kind]['sha256']
            Path(directory,filename).write_bytes(payload)
            if os.environ.get('CAD_AGENT_TEST_EVIDENCE_DIR'):
                retained=Path(os.environ['CAD_AGENT_TEST_EVIDENCE_DIR'])/str(modified_run.workflow_id)
                retained.mkdir(parents=True,exist_ok=True)
                (retained/filename).write_bytes(payload)
        verifier = Path(__file__).resolve().parents[1] / 'e2e/chamfer_scope_geometry.py'
        # The Docker daemon sees the shared task directory, not /app inside the
        # backend container. Stage the verifier beside its declared inputs.
        shared_verifier=Path(directory)/'verify.py'
        shared_verifier.write_bytes(verifier.read_bytes())
        process = await asyncio.create_subprocess_exec(
            settings.sandbox_command,'run','--rm','--network','none','--read-only',
            '--tmpfs','/tmp:rw,size=2g','-e','CAD_SCOPE_VERIFY_ARTIFACTS=1',
            '-v',f'{directory}:/sandbox/input:ro',
            '--entrypoint','/opt/freecad/bin/FreeCADCmd',settings.sandbox_image,
            '-c',"exec(compile(open('/sandbox/input/verify.py').read(), '/sandbox/input/verify.py', 'exec'))",
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,
        )
        measurement, _ = await process.communicate()
        assert process.returncode == 0 and b'CAD_CHAMFER_SCOPE=' in measurement, measurement.decode()

    modified_change_set_id = UUID(modify_result["change_set_id"])
    modified_accepted = await accept_change_set(
        tenant_id=owner.tenant_id,
        reviewer_principal_id=owner.principal_id,
        change_set_id=modified_change_set_id,
        review_note="真实 FreeCAD 修改回归通过",
    )
    assert modified_accepted.status == "accepted"
    modified_committed = await commit_change_set(
        tenant_id=owner.tenant_id,
        reviewer_principal_id=owner.principal_id,
        change_set_id=modified_change_set_id,
    )
    assert modified_committed.status == "committed"
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        modified_head = await connection.scalar(
            text("SELECT head_revision_id FROM project_branches WHERE id=:id"),
            {"id": initial.branch_id},
        )
    assert str(modified_head) == modify_result["candidate_revision_id"]
    current = await document_snapshot(owner,initial.branch_id)
    current_hole = next(f for f in current['features'] if f['id']==hole_feature['id'])
    assert current_hole['annotation_version']==2
    assert_centered_cut(current_hole, 4)
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as connection:
        reports=(await connection.execute(text("SELECT outcome,evidence FROM agent_validation_evidence WHERE workflow_run_id IN (:generated,:modified) AND gate='dfm'"),
            {'generated':created.workflow_id,'modified':modified_run.workflow_id})).mappings().all()
    assert len(reports)==2
    for row in reports:
        report=row['evidence']
        assert {'fdm_bridge_distance','fdm_min_feature'}.issubset(report['evaluated_rule_ids']),report
        assert not report['unevaluated_rule_ids'],report
        assert report['metrics']['min_feature_size_mm']>0,report
        assert report['runtime_provenance'],report
    print('CAD_FUSED_DFM_REPORT='+json.dumps([dict(r) for r in reports],ensure_ascii=False,default=str))

    stale_objective = "Change the existing hole diameter to 9 mm."
    stale_payload = {
        "branch_id": str(initial.branch_id),
        "expected_base_revision_id": str(generated_revision_id),
        "operation": "modify",
        "modeling_backend": "freecad",
        "objective": stale_objective,
    }
    # Already-stale requests are now rejected by the atomic document enqueue,
    # before starting Temporal. The separate persisted-stale test covers a
    # request that becomes stale after it was queued.
    from app.services.cloud_documents import DocumentConflict

    stale_key = f"agent-v2-freecad-stale-real-{project_id}"
    with pytest.raises(DocumentConflict):
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
            await create_workflow(
                connection,
                tenant_id=owner.tenant_id,
                project_id=project_id,
                requested_by_principal_id=owner.principal_id,
                kind="mcad.agent.v2.modify",
                idempotency_key=stale_key,
                request_payload=stale_payload,
            )
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as connection:
        assert await connection.scalar(text(
            "SELECT count(*) FROM workflow_runs WHERE project_id=:project AND idempotency_key=:key"
        ), {"project": project_id, "key": stale_key}) == 0


class _V2PlannerStub:
    def __init__(self):
        self.calls = 0

    async def plan_new(self, messages):
        from app.models.schemas import CADPlan, DesignBrief

        self.calls += 1
        return CADPlan(
            description="创建 20x10x4 mm 安装支架",
            part_type="bracket",
            dimensions={"length": 20, "width": 10, "height": 4},
            features=["两个安装孔"],
            constraints=[],
            modeling_hint="extrude_cut",
            design_brief=DesignBrief(
                intent_summary="创建安装支架",
                artifact_type="bracket",
                open_questions=["必须先确认安装孔中心距，否则无法安全默认"],
            ),
        )


class _V2SolidBoxPlannerStub(_V2PlannerStub):
    async def plan_new(self, messages):
        from app.models.schemas import CADPlan, DesignBrief
        self.calls += 1
        return CADPlan(description="创建 20x10x4 mm 实心长方体", part_type="box",
            dimensions={"length":20,"width":10,"height":4}, features=[], constraints=[],
            modeling_hint="extrude", design_brief=DesignBrief(
                intent_summary="创建无孔、无圆角的实心长方体", artifact_type="box"))


class _V2DecomposerStub:
    async def decompose(self, plan, *, allow_fallback=True):
        return BuildPlan(steps=[], complexity="simple")


class _V2AssemblyStub:
    async def plan_assembly(self, plan, *, allow_fallback=True):
        return AssemblyPlan(parts=[], assembly_description="unused")


class _V2AssemblyPlannerStub:
    async def plan_new(self, messages):
        from app.models.schemas import CADPlan, DesignBrief

        return CADPlan(
            description="创建底座与上盖装配体",
            part_type="assembly",
            dimensions={"length": 30, "width": 20, "height": 8},
            features=["底座", "上盖"],
            constraints=[],
            modeling_hint="assembly_combine",
            design_brief=DesignBrief(
                intent_summary="创建底座与上盖装配体",
                artifact_type="assembly",
            ),
        )


class _V2AssemblyDecompositionStub:
    async def plan_assembly(self, plan, *, allow_fallback=True):
        return AssemblyPlan(
            assembly_description="底座与上盖上下装配",
            parts=[
                AssemblyPart(
                    name="base",
                    description="创建 30x20x5 mm 底座",
                    dimensions={"length": 30, "width": 20, "height": 5},
                    position=[0, 0, 0],
                    color="lightgray",
                ),
                AssemblyPart(
                    name="lid",
                    description="创建 30x20x3 mm 上盖",
                    dimensions={"length": 30, "width": 20, "height": 3},
                    position=[0, 0, 5],
                    color="steelblue",
                ),
            ],
        )


class _V2AssemblyModelingStub:
    def __init__(self, fail_step: str | None = None):
        self.fail_step = fail_step
        self.calls: list[str] = []

    async def generate_step_source(self, *, step, requirements, **_kwargs):
        self.calls.append(step.step_key)
        if step.step_key == self.fail_step:
            return SourceGenerationResult(
                source_code="raise ValueError('controlled part failure')\n",
                mode="3d",
                generator_kind="controlled_assembly_failure",
                provenance={
                    "provider": "controlled-provider",
                    "model": "controlled-model",
                    "provider_response_id": f"completion-{step.step_key}",
                    "request_hash": "e" * 64,
                    "response_hash": "f" * 64,
                    "finish_reason": "stop",
                    "usage": {},
                },
            )
        if step.kind == "assembly_part":
            height = 5 if step.step_key == "part-01" else 3
            source = (
                "import cadquery as cq\n"
                f"def make_{step.step_key.replace('-', '_')}():\n"
                f"    return cq.Workplane('XY').box(30, 20, {height})\n"
                f"result = make_{step.step_key.replace('-', '_')}()\n"
            )
        else:
            assert len(requirements["part_sources"]) == 2
            # Only the provider boundary is controlled in this integration
            # fixture.  The combine step must exercise the production
            # deterministic combiner so the workflow and native BOM runner
            # share the documented bottom-face-centre position contract.
            from app.agent.code_gen import CodeGenerator

            source = await CodeGenerator().generate_assembly_combiner([
                {
                    "name": item["function_name"],
                    "label": item["part_name"],
                    "code": item["source_code"],
                    "position": item["position"],
                    "color": item["color"],
                }
                for item in requirements["part_sources"]
            ])
        return SourceGenerationResult(
            source_code=source,
            mode="3d",
            generator_kind="controlled_assembly",
            provenance={
                "provider": "controlled-provider",
                "model": "controlled-model",
                "provider_response_id": f"completion-{step.step_key}",
                "request_hash": "e" * 64,
                "response_hash": hashlib.sha256(source.encode()).hexdigest(),
                "finish_reason": "stop",
                "usage": {"total_tokens": 16},
            },
        )


class _BlockingAssemblyModelingStub(_V2AssemblyModelingStub):
    def __init__(self):
        super().__init__()
        self.parts_entered = asyncio.Event()
        self.release_parts = asyncio.Event()
        self.active = 0

    async def generate_step_source(self, *, step, **kwargs):
        if step.kind == "assembly_part":
            self.active += 1
            if self.active == 2:
                self.parts_entered.set()
            try:
                await self.release_parts.wait()
            finally:
                self.active -= 1
        return await super().generate_step_source(step=step, **kwargs)


class _V2ComplexPlannerStub:
    async def plan_new(self, messages):
        from app.models.schemas import CADPlan, DesignBrief

        return CADPlan(
            description="创建带安装孔的复杂支架",
            part_type="custom",
            dimensions={"length": 30, "width": 20, "height": 5},
            features=["底板", "安装孔", "圆角"],
            constraints=[],
            modeling_hint="multi_step",
            design_brief=DesignBrief(
                intent_summary="创建复杂安装支架",
                artifact_type="bracket",
            ),
        )


class _V2ComplexDecomposerStub:
    async def decompose(self, plan, *, allow_fallback=True):
        return BuildPlan(
            complexity="complex",
            steps=[
                BuildStep(
                    phase=BuildPhase.BASE,
                    description="创建 30x20x5 mm 底板",
                ),
                BuildStep(
                    phase=BuildPhase.SECONDARY,
                    description="添加直径 4 mm 安装孔",
                ),
            ],
        )


class _ComplexCodeGeneratorStub:
    def __init__(self):
        self.calls: list[int] = []
        self.second_entered = asyncio.Event()
        self.release_second = asyncio.Event()

    async def generate_step(
        self,
        description,
        accumulated,
        step_index,
        total_steps,
        plan_context="",
    ):
        self.calls.append(step_index)
        if step_index == 1:
            self.second_entered.set()
            await self.release_second.wait()
            return "result = result.faces('>Z').workplane().hole(4)"
        return "result = cq.Workplane('XY').box(30, 20, 5)"


class _UnusedRetriever:
    async def find_similar(self, query, top_k=3, **kwargs):
        raise AssertionError("complex step generation should not retrieve examples")


def _controlled_provenance():
    return {
        "provider": "controlled-provider",
        "model": "controlled-complex-model",
        "provider_response_id": "completion-complex",
        "request_hash": "c" * 64,
        "response_hash": "d" * 64,
        "finish_reason": "stop",
        "usage": {"total_tokens": 32},
    }


class _V2ModelingStub:
    def __init__(self):
        self.calls = 0

    async def generate_step_source(self, **_kwargs):
        self.calls += 1
        source = (
            "import cadquery as cq\n"
            "length = 20\nwidth = 10\nheight = 4\n"
            "result = cq.Workplane('XY').box(length, width, height)\n"
        )
        return SourceGenerationResult(
            source_code=source,
            mode="3d",
            generator_kind="controlled_integration",
            provenance={
                "provider": "controlled-provider",
                "model": "controlled-model",
                "provider_response_id": "completion-integration-1",
                "request_hash": "a" * 64,
                "response_hash": hashlib.sha256(source.encode()).hexdigest(),
                "finish_reason": "stop",
                "usage": {"total_tokens": 24},
            },
        )


class _V2BrokenModelingStub:
    def __init__(self):
        self.calls = 0

    async def generate_step_source(self, **_kwargs):
        self.calls += 1
        source = (
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(20, 10)\n"
        )
        return SourceGenerationResult(
            source_code=source,
            mode="3d",
            generator_kind="controlled_invalid_source",
            provenance={
                "provider": "controlled-provider",
                "model": "controlled-model",
                "provider_response_id": "completion-invalid-1",
                "request_hash": "1" * 64,
                "response_hash": hashlib.sha256(source.encode()).hexdigest(),
                "finish_reason": "stop",
                "usage": {"total_tokens": 12},
            },
        )


class _V2DimensionMismatchModelingStub:
    def __init__(self):
        self.calls = 0

    async def generate_step_source(self, **_kwargs):
        self.calls += 1
        source = (
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(12, 10, 4)\n"
        )
        return SourceGenerationResult(
            source_code=source,
            mode="3d",
            generator_kind="controlled_dimension_mismatch",
            provenance={
                "provider": "controlled-provider",
                "model": "controlled-model",
                "provider_response_id": "completion-dimension-mismatch-1",
                "request_hash": "4" * 64,
                "response_hash": hashlib.sha256(source.encode()).hexdigest(),
                "finish_reason": "stop",
                "usage": {"total_tokens": 12},
            },
        )


class _V2RepairStub:
    def __init__(self):
        self.calls = 0

    async def repair(self, *, failure, decision, **_kwargs):
        self.calls += 1
        source = (
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(20, 10, 4)\n"
        )
        return DurableRepairResult(
            source_code=source,
            failure_class=decision.failure_class,
            strategy=decision.strategy,
            provenance={
                "provider": "controlled-repair-provider",
                "model": "controlled-repair-model",
                "provider_response_id": "repair-completion-1",
                "request_hash": "2" * 64,
                "response_hash": hashlib.sha256(source.encode()).hexdigest(),
                "finish_reason": "stop",
                "usage": {"total_tokens": 18},
            },
        )


class _V2StillBrokenRepairStub(_V2RepairStub):
    async def repair(self, *, failure, decision, **_kwargs):
        self.calls += 1
        source = (
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').box(20, 10)\n"
            "# repaired provider response remains invalid\n"
        )
        return DurableRepairResult(
            source_code=source,
            failure_class=decision.failure_class,
            strategy=decision.strategy,
            provenance={
                "provider": "controlled-repair-provider",
                "model": "controlled-repair-model",
                "provider_response_id": "repair-completion-invalid-1",
                "request_hash": "3" * 64,
                "response_hash": hashlib.sha256(source.encode()).hexdigest(),
                "finish_reason": "stop",
                "usage": {"total_tokens": 18},
            },
        )


class _V2PassingVisualStub:
    async def judge(self, **_kwargs):
        return (
            VisualJudgment(is_match=True, confidence=0.99),
            {
                "provider": "controlled-vision-provider",
                "model": "controlled-vision-model",
                "provider_response_id": "vision-completion-1",
                "request_hash": "5" * 64,
                "response_hash": "6" * 64,
                "finish_reason": "stop",
                "usage": {"total_tokens": 20},
            },
        )

    async def repair(self, **_kwargs):
        raise AssertionError("passing visual judgment must not invoke repair")

    async def report(self, *, renders, runtime_provenance, **kwargs):
        judgment, provenance = await self.judge(**kwargs)
        return DurableVisualReport(
            schema_version="durable-visual-report.v1",
            outcome="passed",
            renders=renders,
            judgment=judgment,
            runtime_provenance=runtime_provenance,
            provider_provenance=provenance,
        )


class _NativeBudgetRequirements:
    async def plan_new(self, messages, *, require_acceptance=False):
        from app.models.schemas import CADPlan, DesignBrief
        from app.contracts.acceptance import AcceptanceContract
        objective=messages[-1]['content']
        contract=AcceptanceContract.model_validate({'objective':objective,'checks':[
            {'check_id':'hole-count','kind':'hole_count','nominal':1,'description':'round hole count','source_quote':objective},
            {'check_id':'hole-diameter','kind':'hole_diameter','nominal':6,'description':'round shaft diameter','source_quote':objective},
            {'check_id':'hole-depth','kind':'hole_depth','nominal':8,'description':'through depth','source_quote':objective},
            {'check_id':'hole-position','kind':'hole_position','description':'centered hole','source_quote':objective,
             'scope':{'frame':'bounds_center','axis':[0,0,1],'centers_mm':[[0,0,0]]}},
        ]}) if require_acceptance else None
        return CADPlan(description=objective, part_type='plate',
            dimensions={'length':60,'width':40,'thickness':8},
            features=['through_hole:diameter=6,position=centered'], constraints=[],
            modeling_hint='extrude_cut', design_brief=DesignBrief(intent_summary=messages[-1]['content'],
                artifact_type='plate',acceptance=contract,open_questions=['必须先确认此受控负例计划']))


class _NativeBudgetPlanner(DurableAgentPlanner):
    def __init__(self, mode):
        super().__init__(planner=_NativeBudgetRequirements())
        self.mode = mode

    def compose_freecad_generation(self, *args, **kwargs):
        from app.agent.durable_plan import ValidationGatePolicy, GateMode
        plan = super().compose_freecad_generation(*args, **kwargs)
        return plan.model_copy(update={'validation_policy':plan.validation_policy.model_copy(update={
            'visual':ValidationGatePolicy(mode=GateMode(self.mode),repair_budget=1)})})


class _InsufficientNativeRepair:
    """Only provider output is controlled; both native executions remain real."""
    def __init__(self):
        from app.freecad.operation_generator import FreeCADOperationGenerator
        self.compiler = FreeCADOperationGenerator()
        self.repairs = 0

    async def generate(self, **kwargs):
        return await self.compiler.generate(**kwargs)

    async def repair(self, *, source_code, **kwargs):
        from app.freecad.contracts import FreeCADOperationPlan
        from app.freecad.operation_generator import FreeCADOperationGenerationResult
        self.repairs += 1
        plan = FreeCADOperationPlan.model_validate_json(source_code).model_copy(update={'document_name':'InsufficientVisualRepair'})
        source = plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan,source_code=source,
            generator_kind='controlled_negative_repair',provenance={
                **_controlled_provenance(),'provider':'controlled-negative-provider',
                'response_hash':hashlib.sha256(source.encode()).hexdigest()})


class _NativeNegativeVisual:
    """Deterministic negative vision judgments on actual rendered model images."""
    def __init__(self, outcome):
        self.outcome = outcome
        self.judgments = 0

    async def report(self, *, renders, runtime_provenance, **kwargs):
        assert renders and runtime_provenance, 'the renderer and kernel must execute'
        self.judgments += 1
        return DurableVisualReport(schema_version='durable-visual-report.v1',outcome=self.outcome,
            renders=renders,runtime_provenance=runtime_provenance,
            judgment=VisualJudgment(is_match=False,confidence=0.99) if self.outcome=='failed' else None,
            issues=('controlled negative vision: design remains unverified',),
            provider_provenance={**_controlled_provenance(),'provider':'controlled-negative-vision',
                'provider_response_id':f'negative-judgment-{self.judgments}'})


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('mode,outcome,expected_repairs',[
    ('advisory','failed',1),('required','failed',1),('required','indeterminate',0),
])
async def test_native_visual_negative_budget_with_real_kernel(mode, outcome, expected_repairs):
    """T10: real PG/Temporal/FreeCAD/render/S3; controlled provider negatives."""
    from app.services.workflow_admission import create_document_workflow as create_workflow
    owner, project_id, initial = await _seed_project('native-budget-'+mode+'-'+outcome)
    objective = 'Create a 60x40x8 mm plate with one centered 6 mm through hole.'
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        created = await create_workflow(conn,tenant_id=owner.tenant_id,project_id=project_id,
            requested_by_principal_id=owner.principal_id,kind='mcad.agent.v2.generate',
            idempotency_key=f'native-budget-{project_id}',request_payload={'objective':objective})
    request = McadAgentWorkflowV2Request(workflow_run_id=created.workflow_id,tenant_id=owner.tenant_id,
        project_id=project_id,principal_id=owner.principal_id,branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,operation='generate',modeling_backend='freecad',
        objective=objective,confirmation_timeout_seconds=90)
    client = await get_temporal_client()
    native,visual = _InsufficientNativeRepair(),_NativeNegativeVisual(outcome)
    async with build_agent_v2_workflow_worker(client,backend=get_execution_backend(),
            durable_planner=_NativeBudgetPlanner(mode),freecad_operations=native,durable_visual=visual):
        handle = await client.start_workflow('McadAgentWorkflowV2',request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),task_queue=settings.temporal_agent_v2_task_queue)
        await _wait_for_status(owner,created.workflow_id,{'waiting_confirmation'})
        await confirm_mcad_workflow(created.workflow_id,accepted=True,note='Controlled negative with real native execution',
            workflow_kind='mcad.agent.v2.generate')
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(),timeout=240)
        expected_error = 'agent_visual_validation_indeterminate' if outcome == 'indeterminate' else 'agent_visual_validation_failed'
        assert getattr(failure.value.cause, 'type', None) == expected_error
    assert native.repairs==expected_repairs and visual.judgments==expected_repairs+1
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        assert await conn.scalar(text('SELECT status FROM workflow_runs WHERE id=:id'),{'id':created.workflow_id})=='failed'
        assert await conn.scalar(text('SELECT head_revision_id FROM project_branches WHERE id=:id'),{'id':initial.branch_id})==initial.revision_id
        for table,column in (('change_sets','source_workflow_run_id'),('artifacts','workflow_run_id')):
            assert await conn.scalar(text(f'SELECT count(*) FROM {table} WHERE {column}=:id'),{'id':created.workflow_id})==0
        gates = (await conn.execute(text('SELECT gate,mode,outcome,staging_manifest_id FROM agent_validation_evidence WHERE workflow_run_id=:id ORDER BY created_at,id'),{'id':created.workflow_id})).mappings().all()
        attempts = (await conn.execute(text('SELECT s.kind,s.step_key,a.status,a.attempt_number FROM step_runs s JOIN execution_attempts a ON a.step_run_id=s.id WHERE s.workflow_run_id=:id'),{'id':created.workflow_id})).mappings().all()
    geometry = [g for g in gates if g['gate']=='geometry']
    visions = [g for g in gates if g['gate']=='visual']
    assert len(geometry)==expected_repairs+1 and all(g['outcome']=='passed' for g in geometry)
    assert len({g['staging_manifest_id'] for g in geometry})==len(geometry)
    assert len(visions)==len(geometry) and all(g['outcome']==outcome and g['mode']==mode for g in visions)
    # Temporal can retry a timed-out infrastructure attempt without spending a
    # provider repair. The live regression witnessed a heartbeat timeout here.
    # Require exactly one successful, latest fenced attempt per logical step,
    # with all earlier attempts terminal; never equate retries to model repairs.
    for key in {a['step_key'] for a in attempts}:
        step_attempts = [a for a in attempts if a['step_key'] == key]
        winners = [a for a in step_attempts if a['status'] == 'succeeded']
        assert len(winners) == 1 and winners[0]['attempt_number'] == max(a['attempt_number'] for a in step_attempts), step_attempts
        assert all(a['status'] in {'succeeded','failed','cancelled','timed_out'} for a in step_attempts), step_attempts
    print('CAD_GATE_BUDGET='+json.dumps({'workflow_id':str(created.workflow_id),'mode':mode,'outcome':outcome,
        'repairs':native.repairs,'real_native_and_validation_attempts':len(attempts),
        'geometry_checks':len(geometry),'visual_checks':len(visions),
        'retired_infrastructure_attempts':sum(a['status'] != 'succeeded' for a in attempts),
        'head_unchanged':True,'no_submittable_candidate':True}))


class _V2MismatchThenPassingVisualStub(_V2PassingVisualStub):
    def __init__(self):
        self.judgments = 0
        self.repairs = 0

    async def report(self, *, renders, runtime_provenance, **kwargs):
        self.judgments += 1
        is_match = self.judgments > 1
        judgment = VisualJudgment(
            is_match=is_match,
            confidence=0.97,
            issues=() if is_match else ("controlled_visual_mismatch",),
            suggestions=() if is_match else ("preserve geometry and rebuild",),
        )
        return DurableVisualReport(
            schema_version="durable-visual-report.v1",
            outcome="passed" if is_match else "failed",
            renders=renders,
            judgment=judgment,
            issues=judgment.issues,
            runtime_provenance=runtime_provenance,
            provider_provenance={
                "provider": "controlled-vision-provider",
                "model": "controlled-vision-model",
                "provider_response_id": f"vision-completion-{self.judgments}",
                "request_hash": str(self.judgments) * 64,
                "response_hash": str(self.judgments + 1) * 64,
                "finish_reason": "stop",
                "usage": {"total_tokens": 20},
            },
        )

    async def repair(self, *, source_code, **_kwargs):
        self.repairs += 1
        repaired = source_code.rstrip() + "\n# controlled visual repair\n"
        return repaired, {
            "provider": "controlled-repair-provider",
            "model": "controlled-repair-model",
            "provider_response_id": "visual-repair-completion-1",
            "request_hash": "7" * 64,
            "response_hash": hashlib.sha256(repaired.encode()).hexdigest(),
            "finish_reason": "stop",
            "usage": {"total_tokens": 24},
        }


async def _snapshot(owner, workflow_id):
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        workflow = (
            await connection.execute(
                text(
                    """
                    SELECT status, last_event_sequence
                    FROM workflow_runs WHERE id=:id
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().one_or_none()
        attempts = (
            await connection.execute(
                text(
                    """
                    SELECT id, status, attempt_number, error_code
                    FROM execution_attempts
                    WHERE workflow_run_id=:id
                    ORDER BY created_at
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().all()
        artifacts = (
            await connection.execute(
                text(
                    """
                    SELECT filename, object_key, size_bytes, sha256
                    FROM artifacts WHERE workflow_run_id=:id
                    ORDER BY filename
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().all()
        change_set = (
            await connection.execute(
                text(
                    """
                    SELECT c.status, c.candidate_revision_id,
                           c.validation_summary,
                           b.head_revision_id
                    FROM change_sets c
                    JOIN project_branches b ON b.id=c.branch_id
                    WHERE c.source_workflow_run_id=:id
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().one_or_none()
        events = (
            await connection.execute(
                text(
                    """
                    SELECT sequence, event_type, payload
                    FROM task_events WHERE workflow_run_id=:id
                    ORDER BY sequence
                    """
                ),
                {"id": workflow_id},
            )
        ).mappings().all()
    return {
        "workflow": workflow,
        "attempts": attempts,
        "artifacts": artifacts,
        "change_set": change_set,
        "events": events,
    }


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _start_api(env: dict[str, str], port: int):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        cwd=str(ROOT),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    deadline = asyncio.get_running_loop().time() + 20
    async with httpx.AsyncClient() as client:
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise AssertionError("FastAPI exited during startup")
            try:
                response = await client.get(
                    f"http://127.0.0.1:{port}/health",
                    timeout=1,
                )
                if response.status_code == 200:
                    assert response.json()["status"] == "ok"
                    return process
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    process.kill()
    await process.wait()
    raise AssertionError("FastAPI did not become ready")


@pytest.mark.asyncio(loop_scope="module")
async def test_real_geometry_capability_reports_valid_and_corrupt_artifacts(
    tmp_path,
):
    import json
    import trimesh

    valid_stl = tmp_path / "valid.stl"
    trimesh.creation.box(extents=(20, 10, 4)).export(valid_stl)
    corrupt_step = tmp_path / "corrupt.step"
    corrupt_step.write_bytes(b"not a STEP artifact")
    adapter = CapabilityExecutionAdapter(get_execution_backend())

    valid = await adapter.execute(
        capability="geometry",
        operation="validate",
        request_id=f"geometry-valid-{uuid4()}",
        params={
            "artifacts": [{"role": "stl", "format": "stl"}],
            "expected_dimensions_mm": {
                "length": 20,
                "width": 10,
                "height": 4,
            },
            "dimension_tolerance": 0.05,
            "output": "geometry-report.json",
        },
        inputs={"stl": valid_stl},
        artifact_media_type="application/json",
        mode="analysis",
        timeout_seconds=120,
        output_bytes=4 * 1024 * 1024,
    )
    invalid = await adapter.execute(
        capability="geometry",
        operation="validate",
        request_id=f"geometry-corrupt-{uuid4()}",
        params={
            "artifacts": [{"role": "step", "format": "step"}],
            "expected_dimensions_mm": {},
            "dimension_tolerance": 0.05,
            "output": "geometry-report.json",
        },
        inputs={"step": corrupt_step},
        artifact_media_type="application/json",
        mode="analysis",
        timeout_seconds=120,
        output_bytes=4 * 1024 * 1024,
    )

    assert valid.execution.result.status is ExecutionStatus.SUCCEEDED
    assert invalid.execution.result.status is ExecutionStatus.SUCCEEDED
    valid_report = json.loads(
        valid.execution.files["artifact"].read_text(encoding="utf-8")
    )
    invalid_report = json.loads(
        invalid.execution.files["artifact"].read_text(encoding="utf-8")
    )
    assert valid_report["outcome"] == "passed"
    assert invalid_report["outcome"] == "indeterminate"
    assert invalid_report["artifacts"][0]["parseable"] is False


def _box_execution(*, delay_seconds: int = 0) -> McadExecutionRequest:
    delay = (
        f"for _ in range({delay_seconds * 10_000_000}):\n"
        "    pass\n"
        if delay_seconds
        else ""
    )
    return McadExecutionRequest(
        step_key="model",
        kind="mcad_model",
        operation="generate",
        mode="3d",
        source_code=(
            f"{delay}import cadquery as cq\n"
            "result = cq.Workplane('XY').box(20, 10, 5)\n"
        ),
        timeout_seconds=60,
    )


def _dxf_execution() -> McadExecutionRequest:
    return McadExecutionRequest(
        step_key="export-dxf",
        kind="mcad_export",
        operation="export",
        mode="2d",
        source_code=(
            "import ezdxf\n"
            "doc = ezdxf.new()\n"
            "doc.units = ezdxf.units.MM\n"
            "msp = doc.modelspace()\n"
            "msp.add_lwpolyline([(0,0),(20,0),(20,10),(0,10)], close=True)\n"
            "doc.saveas('/sandbox/output/result.dxf')\n"
        ),
        timeout_seconds=30,
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_plan_waits_before_candidate_source_or_execution():
    owner, project_id, initial = await _seed_project("agent-v2-confirm")
    client = await get_temporal_client()
    planner_stub = _V2PlannerStub()
    planner = DurableAgentPlanner(
        planner=planner_stub,
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    request_payload = {
        "branch_id": str(initial.branch_id),
        "expected_base_revision_id": str(initial.revision_id),
        "operation": "generate",
        "objective": "创建 20x10x4 mm 安装支架",
    }
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-{project_id}",
            request_payload=request_payload,
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective=request_payload["objective"],
        confirmation_timeout_seconds=60,
    )

    async with build_agent_v2_workflow_worker(
        client,
        backend=object(),
        durable_planner=planner,
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        assert (
            await _wait_for_status(
                owner,
                created.workflow_id,
                {"waiting_confirmation"},
            )
            == "waiting_confirmation"
        )
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            counts = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM change_sets "
                        " WHERE source_workflow_run_id=:id) AS candidates, "
                        "(SELECT count(*) FROM execution_attempts "
                        " WHERE workflow_run_id=:id) AS attempts, "
                        "(SELECT count(*) FROM artifacts "
                        " WHERE workflow_run_id=:id) AS artifacts"
                    ),
                    {"id": created.workflow_id},
                )
            ).mappings().one()
        assert dict(counts) == {
            "candidates": 0,
            "attempts": 0,
            "artifacts": 0,
        }
        assert planner_stub.calls == 1
        await confirm_mcad_workflow(
            created.workflow_id,
            accepted=False,
            note="拒绝当前计划",
            workflow_kind="mcad.agent.v2.generate",
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
        assert result["status"] == "cancelled"

    snapshot = await _snapshot(owner, created.workflow_id)
    assert snapshot["workflow"]["status"] == "cancelled"
    assert snapshot["attempts"] == []
    assert snapshot["artifacts"] == []
    assert snapshot["change_set"] is None


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_confirmed_plan_seals_reviewable_candidate():
    owner, project_id, initial = await _seed_project("agent-v2-candidate")
    client = await get_temporal_client()
    planner = DurableAgentPlanner(
        planner=_V2PlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    modeling = _V2ModelingStub()
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-candidate-{project_id}",
            request_payload={"objective": "创建 20x10x4 mm 安装支架"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建 20x10x4 mm 安装支架",
        confirmation_timeout_seconds=60,
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=modeling,
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await _wait_for_status(
            owner,
            created.workflow_id,
            {"waiting_confirmation"},
        )
        await confirm_mcad_workflow(
            created.workflow_id,
            accepted=True,
            note="确认执行",
            workflow_kind="mcad.agent.v2.generate",
        )
        result = await asyncio.wait_for(handle.result(), timeout=90)
        assert result["status"] == "succeeded"

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        candidate = (
            await connection.execute(
                text(
                    "SELECT status, failure_code, failure_message, candidate_revision_id, "
                    "change_set_id FROM agent_candidate_builds "
                    "WHERE workflow_run_id=:id"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one()
        workflow_status = await connection.scalar(
            text("SELECT status FROM workflow_runs WHERE id=:id"),
            {"id": created.workflow_id},
        )
        attempt_count = await connection.scalar(
            text("SELECT count(*) FROM execution_attempts WHERE workflow_run_id=:id"),
            {"id": created.workflow_id},
        )
        source = (
            await connection.execute(
                text(
                    "SELECT source_hash, provider, model, provider_response_id, "
                    "request_hash, response_hash FROM agent_generated_sources "
                    "WHERE workflow_run_id=:id"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one()
        manifest = (
            await connection.execute(
                text(
                    "SELECT id, manifest FROM agent_staging_manifests "
                    "WHERE workflow_run_id=:id"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one()
        product_artifacts = await connection.scalar(
            text("SELECT count(*) FROM artifacts WHERE workflow_run_id=:id"),
            {"id": created.workflow_id},
        )
        geometry_evidence = (
            await connection.execute(
                text(
                    "SELECT outcome, evidence FROM agent_validation_evidence "
                    "WHERE workflow_run_id=:id AND gate='geometry'"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one()
        validation_evidence = (
            await connection.execute(
                text(
                    "SELECT gate, mode, outcome, evidence "
                    "FROM agent_validation_evidence "
                    "WHERE workflow_run_id=:id ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        selected_steps = list(
            (
                await connection.execute(
                    text(
                        "SELECT sm.plan_step_key FROM agent_seal_manifests sm "
                        "JOIN agent_candidate_seals s ON s.id=sm.seal_id "
                        "WHERE s.candidate_build_id=(SELECT id FROM "
                        "agent_candidate_builds WHERE workflow_run_id=:id) "
                        "ORDER BY sm.ordinal"
                    ),
                    {"id": created.workflow_id},
                )
            ).scalars()
        )
    assert candidate["status"] == "reviewable", candidate["failure_message"]
    assert candidate["failure_code"] is None
    assert candidate["candidate_revision_id"] is not None
    assert candidate["change_set_id"] is not None
    assert workflow_status == "succeeded"
    assert attempt_count == 4
    assert modeling.calls == 1
    assert source["provider"] == "controlled-provider"
    assert source["model"] == "controlled-model"
    assert source["provider_response_id"] == "completion-integration-1"
    assert len(source["request_hash"]) == 64
    assert source["response_hash"] == source["source_hash"]
    assert len(manifest["manifest"]["outputs"]) == 2
    assert all(item["size_bytes"] > 0 for item in manifest["manifest"]["outputs"])
    assert geometry_evidence["outcome"] == "passed"
    assert geometry_evidence["evidence"]["runtime_provenance"]["image_digest"]
    assert [item["gate"] for item in validation_evidence] == [
        "geometry",
        "visual",
        "dfm",
    ]
    assert validation_evidence[1]["mode"] == "advisory"
    assert validation_evidence[1]["outcome"] == "indeterminate"
    assert len(validation_evidence[1]["evidence"]["renders"]) == 4
    assert len(validation_evidence[1]["evidence"]["issues"]) == 1
    assert validation_evidence[1]["evidence"]["issues"][0].startswith(
        "vision_provider_unavailable:"
    )
    assert validation_evidence[1]["evidence"]["provider_provenance"] is None
    assert validation_evidence[2]["mode"] == "advisory"
    assert validation_evidence[2]["outcome"] == "passed"
    assert validation_evidence[2]["evidence"]["unevaluated_rule_ids"] == []
    assert validation_evidence[2]["evidence"]["violations"] == []
    assert validation_evidence[2]["evidence"]["policy_hash"]
    assert validation_evidence[2]["evidence"]["policy_object"]["rules"]
    assert validation_evidence[2]["evidence"]["policy_object"][
        "policy_hash"
    ] == validation_evidence[2]["evidence"]["policy_hash"]
    assert validation_evidence[2]["evidence"]["runtime_provenance"][
        "image_digest"
    ]
    assert product_artifacts == 7
    assert selected_steps == ["model-main"]
    snapshot = await get_task_snapshot(owner, created.workflow_id)
    assert snapshot["agent"]["current_stage"] == "review"
    assert snapshot["agent"]["current_status"] == "reviewable"
    assert [item["gate"] for item in snapshot["agent"]["validations"]] == [
        "geometry",
        "visual",
        "dfm",
    ]
    assert snapshot["agent"]["risk_summary"]["status"] == (
        "attention_required"
    )
    page = await read_task_events(
        owner,
        created.workflow_id,
        after_sequence=0,
        limit=500,
    )
    sequences = [item["sequence"] for item in page["events"]]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    projected = {
        item["event_type"]: item["projection"] for item in page["events"]
    }
    assert projected["agent.plan.completed"]["stage"] == "planning"
    assert projected["agent.source.generated"]["stage"] == "modeling"
    validation_projections = [
        item["projection"] for item in page["events"]
        if item["event_type"] == "agent.validation_evidence.recorded"
    ]
    assert [item["status"] for item in validation_projections] == [
        "success",
        "warn",
        "success",
    ]
    assert projected["agent.candidate.sealed"]["stage"] == "review"
    assert projected["agent.candidate.sealed"]["risk_count"] == 1
    change_detail = await get_change_set_detail(
        owner,
        candidate["change_set_id"],
    )
    assert change_detail["risk_summary"]["issue_count"] == 1
    assert any(
        event["event_type"] == "agent.candidate.sealed"
        and event["projection"]["stage"] == "review"
        for event in change_detail["agent_events"]
    )


@pytest.mark.skipif(
    os.environ.get("CAD_AGENT_TEST_REAL_VISION") != "1",
    reason="CAD_AGENT_TEST_REAL_VISION=1 is required for vision integration",
)
@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_real_visual_provider_persists_provenance():
    owner, project_id, initial = await _seed_project("agent-v2-real-vision")
    client = await get_temporal_client()
    planner = DurableAgentPlanner(
        planner=_V2SolidBoxPlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-real-vision-{project_id}",
            request_payload={"objective": "创建 20x10x4 mm 实心长方体"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建 20x10x4 mm 实心长方体，不需要孔、圆角或其他特征",
        confirmation_timeout_seconds=60,
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=_V2ModelingStub(),
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        # This fully specified box may proceed without missing-input confirmation.
        # Pre-send the test's approval so either actual planner decision can run.
        await handle.signal("confirmation", {"accepted": True, "note": "确认真实视觉服务测试"})
        result = await asyncio.wait_for(handle.result(), timeout=180)
        assert result["status"] == "succeeded"

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        visual_rows = (
            await connection.execute(
                text(
                    "SELECT outcome, evidence FROM agent_validation_evidence "
                    "WHERE workflow_run_id=:id AND gate='visual' "
                    "ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        if not visual_rows:
            failure = (
                await connection.execute(
                    text(
                        "SELECT w.error_code, w.error_message, c.failure_code, "
                        "c.failure_message FROM workflow_runs w "
                        "LEFT JOIN agent_candidate_builds c "
                        "ON c.workflow_run_id=w.id WHERE w.id=:id"
                    ),
                    {"id": created.workflow_id},
                )
            ).mappings().one()
            attempts = (
                await connection.execute(
                    text(
                        "SELECT s.kind, a.status, a.error_code "
                        "FROM execution_attempts a JOIN step_runs s "
                        "ON s.id=a.step_run_id WHERE a.workflow_run_id=:id "
                        "ORDER BY a.created_at"
                    ),
                    {"id": created.workflow_id},
                )
            ).mappings().all()
            raise AssertionError(
                f"visual evidence missing: failure={dict(failure)}; "
                f"attempts={[dict(item) for item in attempts]}"
            )
    for visual in visual_rows:
        provenance = visual["evidence"]["provider_provenance"]
        assert visual["outcome"] in {"passed", "failed"}
        assert provenance["provider"] == settings.normalized_llm_provider
        assert provenance["model"] == settings.effective_vision_model
        assert provenance["provider_response_id"]
        assert len(provenance["request_hash"]) == 64
        assert len(provenance["response_hash"]) == 64


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_visual_mismatch_repairs_and_revalidates_geometry():
    owner, project_id, initial = await _seed_project("agent-v2-visual-repair")
    client = await get_temporal_client()
    planner = DurableAgentPlanner(
        planner=_V2PlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    visual = _V2MismatchThenPassingVisualStub()
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-visual-repair-{project_id}",
            request_payload={"objective": "创建 20x10x4 mm 安装支架"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建 20x10x4 mm 安装支架",
        confirmation_timeout_seconds=60,
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=_V2ModelingStub(),
        durable_visual=visual,
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await _wait_for_status(owner, created.workflow_id, {"waiting_confirmation"})
        await confirm_mcad_workflow(
            created.workflow_id,
            accepted=True,
            note="确认视觉修复测试",
            workflow_kind="mcad.agent.v2.generate",
        )
        result = await asyncio.wait_for(handle.result(), timeout=180)
        assert result["status"] == "succeeded"

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        sources = (
            await connection.execute(
                text(
                    "SELECT id, predecessor_source_id, source_hash, provider, model "
                    "FROM agent_generated_sources WHERE workflow_run_id=:id "
                    "ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        manifests = (
            await connection.execute(
                text(
                    "SELECT id, supersedes_id FROM agent_staging_manifests "
                    "WHERE workflow_run_id=:id ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        gates = (
            await connection.execute(
                text(
                    "SELECT gate, outcome, staging_manifest_id, evidence "
                    "FROM agent_validation_evidence WHERE workflow_run_id=:id "
                    "ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        attempts = (
            await connection.execute(
                text(
                    "SELECT s.kind, a.status FROM execution_attempts a "
                    "JOIN step_runs s ON s.id=a.step_run_id "
                    "WHERE a.workflow_run_id=:id ORDER BY a.created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        failure = (
            await connection.execute(
                text(
                    "SELECT w.error_code, w.error_message, c.failure_code, "
                    "c.failure_message FROM workflow_runs w "
                    "LEFT JOIN agent_candidate_builds c "
                    "ON c.workflow_run_id=w.id WHERE w.id=:id"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one()
        selected_manifest_ids = set(
            (
                await connection.execute(
                    text(
                        "SELECT staging_manifest_id FROM agent_seal_manifests sm "
                        "JOIN agent_candidate_seals s ON s.id=sm.seal_id "
                        "WHERE s.candidate_build_id=(SELECT id FROM "
                        "agent_candidate_builds WHERE workflow_run_id=:id)"
                    ),
                    {"id": created.workflow_id},
                )
            ).scalars()
        )
    diagnostic = {
        "failure": dict(failure),
        "sources": [dict(item) for item in sources],
        "manifests": [dict(item) for item in manifests],
        "gates": [dict(item) for item in gates],
        "attempts": [dict(item) for item in attempts],
    }
    assert visual.judgments == 2, json.dumps(
        diagnostic,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
    )
    assert visual.repairs == 1
    assert len(sources) == 2
    assert sources[1]["predecessor_source_id"] == sources[0]["id"]
    assert sources[1]["source_hash"] != sources[0]["source_hash"]
    assert sources[1]["provider"] == "controlled-repair-provider"
    assert len(manifests) == 2
    assert manifests[1]["supersedes_id"] == manifests[0]["id"]
    assert selected_manifest_ids == {manifests[1]["id"]}
    assert [(item["gate"], item["outcome"]) for item in gates] == [
        ("geometry", "passed"),
        ("visual", "failed"),
        ("geometry", "passed"),
        ("visual", "passed"),
        ("dfm", "passed"),
    ], json.dumps(diagnostic, ensure_ascii=False, default=str, sort_keys=True)
    assert gates[-1]["evidence"]["unevaluated_rule_ids"] == []
    assert gates[-1]["evidence"]["violations"] == []
    assert all(item["status"] == "succeeded" for item in attempts)
    assert [item["kind"] for item in attempts] == [
        "agent_model",
        "agent_geometry_validation",
        "agent_visual_render",
        "agent_visual_repair",
        "agent_geometry_validation",
        "agent_visual_render",
        "agent_dfm_validation",
    ]


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_user_code_failure_creates_durable_repair_attempt():
    owner, project_id, initial = await _seed_project("agent-v2-repair")
    client = await get_temporal_client()
    planner = DurableAgentPlanner(
        planner=_V2PlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    modeling = _V2BrokenModelingStub()
    repair = _V2RepairStub()
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-repair-{project_id}",
            request_payload={"objective": "创建 20x10x4 mm 安装支架"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建 20x10x4 mm 安装支架",
        confirmation_timeout_seconds=60,
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=modeling,
        durable_repair=repair,
        durable_visual=_V2PassingVisualStub(),
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await _wait_for_status(
            owner,
            created.workflow_id,
            {"waiting_confirmation"},
        )
        await confirm_mcad_workflow(
            created.workflow_id,
            accepted=True,
            note="确认执行并允许分类修复",
            workflow_kind="mcad.agent.v2.generate",
        )
        result = await asyncio.wait_for(handle.result(), timeout=90)
        assert result["status"] == "succeeded"

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        steps = (
            await connection.execute(
                text(
                    "SELECT id, step_key, kind, status, error_code "
                    "FROM step_runs WHERE workflow_run_id=:id "
                    "AND kind IN ('agent_model', 'agent_repair') "
                    "ORDER BY step_index"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        attempts = (
            await connection.execute(
                text(
                    "SELECT a.status, a.error_code, s.step_key "
                    "FROM execution_attempts a JOIN step_runs s "
                    "ON s.id=a.step_run_id WHERE a.workflow_run_id=:id "
                    "ORDER BY a.created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        sources = (
            await connection.execute(
                text(
                    "SELECT id, predecessor_source_id, source_hash, provider, "
                    "model, provider_response_id FROM agent_generated_sources "
                    "WHERE workflow_run_id=:id ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        manifests = await connection.scalar(
            text(
                "SELECT count(*) FROM agent_staging_manifests "
                "WHERE workflow_run_id=:id"
            ),
            {"id": created.workflow_id},
        )
        repair_event = (
            await connection.execute(
                text(
                    "SELECT payload FROM task_events WHERE workflow_run_id=:id "
                    "AND event_type='agent.repair.source_generated'"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one()

    assert modeling.calls == 1
    assert repair.calls == 1
    assert [(item["kind"], item["status"]) for item in steps] == [
        ("agent_model", "failed"),
        ("agent_repair", "succeeded"),
    ]
    assert [(item["status"], item["error_code"]) for item in attempts] == [
        ("failed", "user_code_failed"),
        ("succeeded", None),
        ("succeeded", None),
        ("succeeded", None),
        ("succeeded", None),
    ]
    assert len(sources) == 2
    assert sources[1]["predecessor_source_id"] == sources[0]["id"]
    assert sources[1]["source_hash"] != sources[0]["source_hash"]
    assert sources[1]["provider"] == "controlled-repair-provider"
    assert sources[1]["model"] == "controlled-repair-model"
    assert sources[1]["provider_response_id"] == "repair-completion-1"
    assert manifests == 1
    assert repair_event["payload"]["prior_source_hash"] == sources[0][
        "source_hash"
    ]
    assert repair_event["payload"]["source_hash"] == sources[1]["source_hash"]


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_geometry_failure_repairs_and_revalidates_new_manifest():
    owner, project_id, initial = await _seed_project("agent-v2-geometry-repair")
    client = await get_temporal_client()
    planner = DurableAgentPlanner(
        planner=_V2PlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    repair = _V2RepairStub()
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-geometry-repair-{project_id}",
            request_payload={"objective": "创建 20x10x4 mm 安装支架"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建 20x10x4 mm 安装支架",
        confirmation_timeout_seconds=60,
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=_V2DimensionMismatchModelingStub(),
        durable_repair=repair,
        durable_visual=_V2PassingVisualStub(),
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await _wait_for_status(
            owner,
            created.workflow_id,
            {"waiting_confirmation"},
        )
        await confirm_mcad_workflow(
            created.workflow_id,
            accepted=True,
            note="确认几何修复测试",
            workflow_kind="mcad.agent.v2.generate",
        )
        result = await asyncio.wait_for(handle.result(), timeout=120)
        assert result["status"] == "succeeded"

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        attempts = (
            await connection.execute(
                text(
                    "SELECT s.kind, a.status, a.error_code FROM execution_attempts a "
                    "JOIN step_runs s ON s.id=a.step_run_id "
                    "WHERE a.workflow_run_id=:id ORDER BY a.created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        manifests = (
            await connection.execute(
                text(
                    "SELECT id, manifest FROM agent_staging_manifests "
                    "WHERE workflow_run_id=:id ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        evidence = (
            await connection.execute(
                text(
                    "SELECT staging_manifest_id, outcome, evidence "
                    "FROM agent_validation_evidence WHERE workflow_run_id=:id "
                    "AND gate='geometry' ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        visual_evidence = (
            await connection.execute(
                text(
                    "SELECT outcome, evidence FROM agent_validation_evidence "
                    "WHERE workflow_run_id=:id AND gate='visual'"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one()
        sources = (
            await connection.execute(
                text(
                    "SELECT id, predecessor_source_id, source_hash "
                    "FROM agent_generated_sources WHERE workflow_run_id=:id "
                    "ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        selected_manifest_ids = set(
            (
                await connection.execute(
                    text(
                        "SELECT staging_manifest_id FROM agent_seal_manifests sm "
                        "JOIN agent_candidate_seals s ON s.id=sm.seal_id "
                        "WHERE s.candidate_build_id=(SELECT id FROM "
                        "agent_candidate_builds WHERE workflow_run_id=:id)"
                    ),
                    {"id": created.workflow_id},
                )
            ).scalars()
        )
    assert repair.calls == 1
    assert [item["kind"] for item in attempts] == [
        "agent_model",
        "agent_geometry_validation",
        "agent_repair",
        "agent_geometry_validation",
        "agent_visual_render",
        "agent_dfm_validation",
    ]
    assert all(item["status"] == "succeeded" for item in attempts)
    assert len(manifests) == 2
    assert [item["outcome"] for item in evidence] == ["failed", "passed"]
    assert evidence[0]["staging_manifest_id"] == manifests[0]["id"]
    assert evidence[1]["staging_manifest_id"] == manifests[1]["id"]
    assert selected_manifest_ids == {manifests[1]["id"]}
    assert "artifact-00:dimension_mismatch" in evidence[0]["evidence"][
        "issues"
    ]
    assert len(sources) == 2
    assert sources[1]["predecessor_source_id"] == sources[0]["id"]
    assert sources[1]["source_hash"] != sources[0]["source_hash"]
    assert visual_evidence["outcome"] == "passed"
    assert visual_evidence["evidence"]["provider_provenance"] == {
        "finish_reason": "stop",
        "model": "controlled-vision-model",
        "provider": "controlled-vision-provider",
        "provider_response_id": "vision-completion-1",
        "request_hash": "5" * 64,
        "response_hash": "6" * 64,
        "usage": {"total_tokens": 20},
    }


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_repeated_repair_failure_stops_without_second_llm_call():
    owner, project_id, initial = await _seed_project("agent-v2-repair-repeat")
    client = await get_temporal_client()
    planner = DurableAgentPlanner(
        planner=_V2PlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    repair = _V2StillBrokenRepairStub()
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-repair-repeat-{project_id}",
            request_payload={"objective": "创建 20x10x4 mm 安装支架"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建 20x10x4 mm 安装支架",
        confirmation_timeout_seconds=60,
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=_V2BrokenModelingStub(),
        durable_repair=repair,
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await _wait_for_status(
            owner,
            created.workflow_id,
            {"waiting_confirmation"},
        )
        await confirm_mcad_workflow(
            created.workflow_id,
            accepted=True,
            note="确认执行",
            workflow_kind="mcad.agent.v2.generate",
        )
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=90)

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        attempts = await connection.scalar(
            text(
                "SELECT count(*) FROM execution_attempts "
                "WHERE workflow_run_id=:id"
            ),
            {"id": created.workflow_id},
        )
        repair_steps = (
            await connection.execute(
                text(
                    "SELECT step_key, status, error_code FROM step_runs "
                    "WHERE workflow_run_id=:id AND kind='agent_repair' "
                    "ORDER BY step_index"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
    assert repair.calls == 1
    assert attempts == 2
    assert [item["step_key"] for item in repair_steps] == [
        "repair-model-main-01"
    ]
    assert repair_steps[0]["status"] == "failed"


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_complex_steps_survive_worker_restart_without_regeneration():
    owner, project_id, initial = await _seed_project("agent-v2-complex-restart")
    client = await get_temporal_client()
    planner = DurableAgentPlanner(
        planner=_V2ComplexPlannerStub(),
        decomposer=_V2ComplexDecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    codegen = _ComplexCodeGeneratorStub()
    modeling = DurableModelingSourceGenerator(
        retriever=_UnusedRetriever(),
        code_generator=codegen,
        provenance_reader=_controlled_provenance,
    )
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-complex-{project_id}",
            request_payload={"objective": "创建带安装孔的复杂支架"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建带安装孔的复杂支架",
        confirmation_timeout_seconds=60,
    )
    backend = get_execution_backend()
    async with build_agent_v2_workflow_worker(
        client,
        backend=backend,
        durable_planner=planner,
        durable_modeling=modeling,
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await asyncio.wait_for(codegen.second_entered.wait(), timeout=60)
        async with tenant_transaction(
            owner.tenant_id,
            owner.principal_id,
        ) as connection:
            first_manifest_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM agent_staging_manifests "
                    "WHERE workflow_run_id=:id"
                ),
                {"id": created.workflow_id},
            )
        assert first_manifest_count == 1

    codegen.release_second.set()
    async with build_agent_v2_workflow_worker(
        client,
        backend=backend,
        durable_planner=planner,
        durable_modeling=modeling,
    ):
        result = await asyncio.wait_for(handle.result(), timeout=90)
        assert result["status"] == "succeeded"

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        steps = (
            await connection.execute(
                text(
                    "SELECT step_key, status, attempt_count FROM step_runs "
                    "WHERE workflow_run_id=:id AND kind='agent_model' "
                    "ORDER BY step_index"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        source_rows = (
            await connection.execute(
                text(
                    "SELECT source_hash, predecessor_source_id "
                    "FROM agent_generated_sources WHERE workflow_run_id=:id "
                    "ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        manifest_count = await connection.scalar(
            text(
                "SELECT count(*) FROM agent_staging_manifests "
                "WHERE workflow_run_id=:id"
            ),
            {"id": created.workflow_id},
        )
        selected_steps = list(
            (
                await connection.execute(
                    text(
                        "SELECT sm.plan_step_key FROM agent_seal_manifests sm "
                        "JOIN agent_candidate_seals s ON s.id=sm.seal_id "
                        "WHERE s.candidate_build_id=(SELECT id FROM "
                        "agent_candidate_builds WHERE workflow_run_id=:id) "
                        "ORDER BY sm.ordinal"
                    ),
                    {"id": created.workflow_id},
                )
            ).scalars()
        )
    assert [row["status"] for row in steps] == ["succeeded", "succeeded"]
    assert [row["attempt_count"] for row in steps] == [1, 1]
    assert len(source_rows) == 2
    assert source_rows[0]["predecessor_source_id"] is None
    assert source_rows[1]["predecessor_source_id"] is not None
    assert manifest_count == 2
    assert codegen.calls.count(0) == 1
    assert selected_steps == ["model-02-secondary"]


async def _run_assembly_case(*, fail_step: str | None):
    owner, project_id, initial = await _seed_project(
        f"agent-v2-assembly-{fail_step or 'success'}"
    )
    client = await get_temporal_client()
    modeling = _V2AssemblyModelingStub(fail_step=fail_step)
    planner = DurableAgentPlanner(
        planner=_V2AssemblyPlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyDecompositionStub(),
    )
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-assembly-{project_id}",
            request_payload={"objective": "创建底座与上盖装配体"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建底座与上盖装配体",
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=modeling,
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        if fail_step is None:
            result = await asyncio.wait_for(handle.result(), timeout=120)
            assert result["status"] == "succeeded"
        else:
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), timeout=120)
    return owner, created.workflow_id, modeling


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_assembly_executes_parts_then_combine_with_source_edges():
    owner, workflow_id, modeling = await _run_assembly_case(fail_step=None)
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        steps = (
            await connection.execute(
                text(
                    "SELECT step_key, status, attempt_count FROM step_runs "
                    "WHERE workflow_run_id=:id AND kind='agent_model' "
                    "ORDER BY step_index"
                ),
                {"id": workflow_id},
            )
        ).mappings().all()
        edges = (
            await connection.execute(
                text(
                    "SELECT i.ordinal, s.step_key AS input_step "
                    "FROM agent_generated_source_inputs i "
                    "JOIN agent_generated_sources g ON g.id=i.input_source_id "
                    "JOIN step_runs s ON s.id=g.step_run_id "
                    "JOIN agent_generated_sources combined ON combined.id=i.source_id "
                    "JOIN step_runs cs ON cs.id=combined.step_run_id "
                    "WHERE i.tenant_id=:tenant_id "
                    "AND cs.workflow_run_id=:id AND cs.step_key='combine' "
                    "ORDER BY i.ordinal"
                ),
                {"tenant_id": owner.tenant_id, "id": workflow_id},
            )
        ).mappings().all()
        manifests = await connection.scalar(
            text(
                "SELECT count(*) FROM agent_staging_manifests "
                "WHERE workflow_run_id=:id"
            ),
            {"id": workflow_id},
        )
        selected_steps = list(
            (
                await connection.execute(
                    text(
                        "SELECT sm.plan_step_key FROM agent_seal_manifests sm "
                        "JOIN agent_candidate_seals s ON s.id=sm.seal_id "
                        "WHERE s.candidate_build_id=(SELECT id FROM "
                        "agent_candidate_builds WHERE workflow_run_id=:id) "
                        "ORDER BY sm.ordinal"
                    ),
                    {"id": workflow_id},
                )
            ).scalars()
        )
        bom_evidence = (
            await connection.execute(
                text(
                    "SELECT outcome, evidence FROM agent_validation_evidence "
                    "WHERE workflow_run_id=:id AND gate='bom'"
                ),
                {"id": workflow_id},
            )
        ).mappings().one()
        bom_artifacts = list(
            (
                await connection.execute(
                    text(
                        "SELECT a.artifact_kind FROM artifacts a "
                        "JOIN agent_candidate_builds b "
                        "ON b.candidate_revision_id=a.revision_id "
                        "WHERE b.workflow_run_id=:id "
                        "AND a.artifact_kind IN ('bom_json', 'bom_csv') "
                        "ORDER BY a.artifact_kind"
                    ),
                    {"id": workflow_id},
                )
            ).scalars()
        )
        sealed_bom_evidence = await connection.scalar(
            text(
                "SELECT count(*) FROM agent_seal_evidence se "
                "JOIN agent_candidate_seals s ON s.id=se.seal_id "
                "JOIN agent_candidate_builds b ON b.id=s.candidate_build_id "
                "WHERE b.workflow_run_id=:id AND se.gate='bom'"
            ),
            {"id": workflow_id},
        )
    assert [row["step_key"] for row in steps] == [
        "part-01",
        "part-02",
        "combine",
    ]
    assert all(row["status"] == "succeeded" for row in steps)
    assert all(row["attempt_count"] == 1 for row in steps)
    assert [row["input_step"] for row in edges] == ["part-01", "part-02"]
    assert manifests == 3
    assert modeling.calls.count("part-01") == 1
    assert selected_steps == ["combine"]
    assert bom_evidence["outcome"] == "passed"
    assert {
        item["role"] for item in bom_evidence["evidence"]["artifacts"]
    } == {"bom-json", "bom-csv"}
    assert bom_artifacts == ["bom_csv", "bom_json"]
    assert sealed_bom_evidence == 1


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_assembly_partial_failure_preserves_successful_part():
    owner, workflow_id, modeling = await _run_assembly_case(fail_step="part-02")
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        steps = (
            await connection.execute(
                text(
                    "SELECT step_key, status FROM step_runs "
                    "WHERE workflow_run_id=:id AND kind='agent_model' "
                    "ORDER BY step_index"
                ),
                {"id": workflow_id},
            )
        ).mappings().all()
        manifests = (
            await connection.execute(
                text(
                    "SELECT s.step_key FROM agent_staging_manifests m "
                    "JOIN step_runs s ON s.id=m.step_run_id "
                    "WHERE m.workflow_run_id=:id"
                ),
                {"id": workflow_id},
            )
        ).scalars().all()
    by_key = {row["step_key"]: row["status"] for row in steps}
    assert by_key["part-01"] == "succeeded"
    assert by_key["part-02"] == "failed"
    assert "combine" not in by_key
    assert manifests == ["part-01"]
    assert "combine" not in modeling.calls


@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_assembly_cancel_stops_active_parts_before_execution():
    owner, project_id, initial = await _seed_project("agent-v2-assembly-cancel")
    client = await get_temporal_client()
    modeling = _BlockingAssemblyModelingStub()
    planner = DurableAgentPlanner(
        planner=_V2AssemblyPlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyDecompositionStub(),
    )
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-assembly-cancel-{project_id}",
            request_payload={"objective": "创建底座与上盖装配体"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建底座与上盖装配体",
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=modeling,
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await asyncio.wait_for(modeling.parts_entered.wait(), timeout=30)
        await handle.signal("cancel_requested", "取消装配体")
        modeling.release_parts.set()
        result = await asyncio.wait_for(handle.result(), timeout=60)
    assert result["status"] == "cancelled"
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        candidate_status = await connection.scalar(
            text(
                "SELECT status FROM agent_candidate_builds "
                "WHERE workflow_run_id=:id"
            ),
            {"id": created.workflow_id},
        )
        attempt_count = await connection.scalar(
            text(
                "SELECT count(*) FROM execution_attempts "
                "WHERE workflow_run_id=:id"
            ),
            {"id": created.workflow_id},
        )
    assert candidate_status == "cancelled"
    assert attempt_count == 0


@pytest.mark.skipif(
    os.environ.get("CAD_AGENT_TEST_REAL_LLM") != "1",
    reason="CAD_AGENT_TEST_REAL_LLM=1 is required for provider integration",
)
@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_real_repair_provider_persists_provenance_and_attempt():
    owner, project_id, initial = await _seed_project("agent-v2-real-repair")
    client = await get_temporal_client()
    planner = DurableAgentPlanner(
        planner=_V2PlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-real-repair-{project_id}",
            request_payload={"objective": "创建 20x10x4 mm 安装支架"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective="创建 20x10x4 mm 安装支架",
        confirmation_timeout_seconds=60,
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_planner=planner,
        durable_modeling=_V2BrokenModelingStub(),
        durable_visual=_V2PassingVisualStub(),
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await _wait_for_status(
            owner,
            created.workflow_id,
            {"waiting_confirmation"},
        )
        await confirm_mcad_workflow(
            created.workflow_id,
            accepted=True,
            note="确认真实修复服务测试",
            workflow_kind="mcad.agent.v2.generate",
        )
        try:
            result = await asyncio.wait_for(handle.result(), timeout=360)
            assert result["status"] == "succeeded"
        except TimeoutError:
            await handle.terminate("real repair provider test timed out")
            raise

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        sources = (
            await connection.execute(
                text(
                    "SELECT id, predecessor_source_id, source_hash, provider, "
                    "model, provider_response_id, request_hash, response_hash "
                    "FROM agent_generated_sources WHERE workflow_run_id=:id "
                    "ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        attempts = (
            await connection.execute(
                text(
                    "SELECT status, error_code FROM execution_attempts "
                    "WHERE workflow_run_id=:id ORDER BY created_at"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().all()
        manifest_count = await connection.scalar(
            text(
                "SELECT count(*) FROM agent_staging_manifests "
                "WHERE workflow_run_id=:id"
            ),
            {"id": created.workflow_id},
        )
    assert 2 <= len(sources) <= 3
    for previous, current in zip(sources, sources[1:]):
        assert current["predecessor_source_id"] == previous["id"]
        assert current["source_hash"] != previous["source_hash"]
        assert current["provider"] == settings.normalized_llm_provider
        assert current["model"] == settings.llm_model
        assert current["provider_response_id"]
        assert len(current["request_hash"]) == 64
        assert len(current["response_hash"]) == 64
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["error_code"] == "user_code_failed"
    assert all(item["status"] == "succeeded" for item in attempts[1:])
    assert len(attempts) in {5, 7}
    assert manifest_count in {1, 2}


@pytest.mark.skipif(
    os.environ.get("CAD_AGENT_TEST_REAL_LLM") != "1",
    reason="CAD_AGENT_TEST_REAL_LLM=1 is required for provider integration",
)
@pytest.mark.asyncio(loop_scope="module")
async def test_agent_v2_real_planner_retriever_codegen_and_execution_provenance():
    owner, project_id, initial = await _seed_project("agent-v2-real-provider")
    client = await get_temporal_client()
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.workflow_admission import create_document_workflow as create_workflow

        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-v2-real-provider-{project_id}",
            request_payload={"objective": "创建校准块"},
        )
    request = McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        operation="generate",
        objective=(
            "创建一个 20 x 10 x 4 mm 的实心长方体校准块，单位 mm；"
            "不需要孔、圆角、倒角或其他特征；输出 STEP 和 STL。"
        ),
        confirmation_timeout_seconds=60,
    )
    async with build_agent_v2_workflow_worker(
        client,
        backend=get_execution_backend(),
        durable_visual=_V2PassingVisualStub(),
    ):
        handle = await client.start_workflow(
            "McadAgentWorkflowV2",
            request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),
            task_queue=settings.temporal_agent_v2_task_queue,
        )
        await handle.signal(
            "confirmation",
            {"accepted": True, "note": "受控真实 provider 测试确认"},
        )
        result = await asyncio.wait_for(handle.result(), timeout=180)
        assert result["status"] == "succeeded"

    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        source = (
            await connection.execute(
                text(
                    "SELECT source_hash, provider, model, provider_response_id, "
                    "request_hash, response_hash FROM agent_generated_sources "
                    "WHERE workflow_run_id=:id ORDER BY created_at DESC LIMIT 1"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one_or_none()
        if source is None:
            workflow_error = (
                await connection.execute(
                    text(
                        "SELECT status, error_code, error_message "
                        "FROM workflow_runs WHERE id=:id"
                    ),
                    {"id": created.workflow_id},
                )
            ).mappings().one()
            events = (
                await connection.execute(
                    text(
                        "SELECT event_type, payload FROM task_events "
                        "WHERE workflow_run_id=:id ORDER BY sequence"
                    ),
                    {"id": created.workflow_id},
                )
            ).mappings().all()
            raise AssertionError(
                f"real provider produced no source: {dict(workflow_error)}; "
                f"events={[dict(item) for item in events]}"
            )
        manifest = (
            await connection.execute(
                text(
                    "SELECT manifest FROM agent_staging_manifests "
                    "WHERE workflow_run_id=:id ORDER BY created_at DESC LIMIT 1"
                ),
                {"id": created.workflow_id},
            )
        ).mappings().one()
        candidate_failure = await connection.scalar(
            text(
                "SELECT failure_code FROM agent_candidate_builds "
                "WHERE workflow_run_id=:id"
            ),
            {"id": created.workflow_id},
        )
    assert source["provider"] == settings.normalized_llm_provider
    assert source["model"]
    assert source["provider_response_id"]
    assert len(source["request_hash"]) == 64
    assert len(source["response_hash"]) == 64
    assert len(source["source_hash"]) == 64
    assert manifest["manifest"]["outputs"]
    assert candidate_failure is None


@pytest.mark.asyncio(loop_scope="module")
async def test_real_plan_model_validate_confirmation_export_and_replay():
    owner, project_id, initial = await _seed_project("complete")
    client = await get_temporal_client()
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"complete-{project_id}",
            objective="生成 20×10×5 mm 盒体并导出 DXF",
            primary=_box_execution(),
            followup=_dxf_execution(),
        )
        assert (
            await _wait_for_status(
                owner,
                workflow_id,
                {"waiting_confirmation"},
            )
            == "waiting_confirmation"
        )
        await confirm_mcad_workflow(
            workflow_id,
            accepted=True,
            note="测试确认",
        )
        result = await asyncio.wait_for(handle.result(), timeout=60)
        assert result["status"] == "succeeded"
        assert result["committed"] is True

        replay_id, replay_handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"complete-{project_id}",
            objective="生成 20×10×5 mm 盒体并导出 DXF",
            primary=_box_execution(),
            followup=_dxf_execution(),
        )
        assert replay_id == workflow_id
        assert (await replay_handle.result())["revision_id"] == result["revision_id"]
        await cancel_mcad_workflow(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            workflow_run_id=workflow_id,
            reason="terminal cancellation is an idempotent no-op",
        )

    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "succeeded"
    assert [row["status"] for row in snapshot["attempts"]] == [
        "succeeded",
        "succeeded",
    ]
    assert len(snapshot["artifacts"]) == 3
    assert snapshot["change_set"]["status"] == "committed"
    assert (
        snapshot["change_set"]["head_revision_id"]
        == snapshot["change_set"]["candidate_revision_id"]
    )
    assert [row["sequence"] for row in snapshot["events"]] == list(
        range(1, len(snapshot["events"]) + 1)
    )
    assert (
        sum(
            row["event_type"] == "workflow.plan_recorded"
            for row in snapshot["events"]
        )
        == 1
    )
    assert any(
        row["event_type"] == "attempt.heartbeat"
        for row in snapshot["events"]
    )
    for artifact in snapshot["artifacts"]:
        payload = await get_object(artifact["object_key"])
        assert len(payload) == artifact["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]


@pytest.mark.asyncio(loop_scope="module")
async def test_real_confirmation_timer_rejects_without_advancing_branch():
    owner, project_id, initial = await _seed_project("timeout")
    client = await get_temporal_client()
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"timeout-{project_id}",
            objective="生成后等待确认超时",
            primary=_box_execution(),
            confirmation_timeout_seconds=1,
        )
        result = await asyncio.wait_for(handle.result(), timeout=60)
    assert result["status"] == "timed_out"
    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "timed_out"
    assert snapshot["change_set"]["status"] == "rejected"
    assert snapshot["change_set"]["head_revision_id"] == initial.revision_id


@pytest.mark.asyncio(loop_scope="module")
async def test_real_cancellation_stops_execution_and_blocks_artifacts():
    owner, project_id, initial = await _seed_project("cancel")
    client = await get_temporal_client()
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"cancel-{project_id}",
            objective="取消长时间建模",
            primary=_box_execution(delay_seconds=20),
        )
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            active = await _snapshot(owner, workflow_id)
            if (
                active["attempts"]
                and active["attempts"][-1]["status"] == "running"
            ):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("execution attempt never reached running")
        await cancel_mcad_workflow(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            workflow_run_id=workflow_id,
            reason="集成测试取消",
        )
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result["status"] == "cancelled"
    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "cancelled"
    assert snapshot["attempts"][-1]["status"] == "cancelled"
    assert snapshot["artifacts"] == []
    assert snapshot["change_set"]["status"] == "rejected"


@pytest.mark.asyncio(loop_scope="module")
async def test_nonretryable_user_code_failure_has_no_false_success():
    owner, project_id, initial = await _seed_project("user-code-failure")
    client = await get_temporal_client()
    invalid = McadExecutionRequest(
        step_key="model",
        kind="mcad_model",
        operation="generate",
        mode="3d",
        source_code="raise ValueError('invalid dimensions')\n",
    )
    async with build_workflow_worker(client):
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="generate",
            idempotency_key=f"user-code-failure-{project_id}",
            objective="验证用户代码失败不会显示成功",
            primary=invalid,
        )
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=30)
    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "failed"
    assert len(snapshot["attempts"]) == 1
    assert snapshot["attempts"][0]["status"] == "failed"
    assert snapshot["attempts"][0]["error_code"] == "user_code_failed"
    assert snapshot["artifacts"] == []
    assert snapshot["change_set"]["status"] == "pending_review"
    assert snapshot["change_set"]["validation_summary"]["status"] == "failed"
    assert snapshot["change_set"]["head_revision_id"] == initial.revision_id


@pytest.mark.asyncio(loop_scope="module")
async def test_stale_base_fails_persisted_workflow_without_execution():
    owner, project_id, initial = await _seed_project("stale-base")
    # Enqueue against a valid head, then advance it before any worker consumes
    # the operation. Already-stale submissions now fail at the API/DB boundary.
    workflow_id, handle = await start_mcad_workflow(
        tenant_id=owner.tenant_id, project_id=project_id,
        principal_id=owner.principal_id, branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id, kind="modify",
        idempotency_key=f"stale-workflow-{project_id}", objective="基于过期版本修改",
        primary=_box_execution(),
    )
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        sibling = await create_candidate_change_set(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            created_by_principal_id=owner.principal_id,
            idempotency_key=f"stale-sibling-{project_id}",
            objective="并发变更",
            candidate_manifest={"state": "advanced"},
        )
        assert await compare_and_swap_branch_head(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            branch_id=initial.branch_id,
            expected_head_revision_id=initial.revision_id,
            candidate_revision_id=sibling.candidate_revision_id,
        )
    client = await get_temporal_client()
    async with build_workflow_worker(client):
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), timeout=30)
    snapshot = await _snapshot(owner, workflow_id)
    assert snapshot["workflow"]["status"] == "failed"
    assert snapshot["attempts"] == []
    assert snapshot["artifacts"] == []
    assert snapshot["change_set"] is None


@pytest.mark.asyncio(loop_scope="module")
async def test_worker_process_crash_retries_with_new_fenced_attempt():
    owner, project_id, initial = await _seed_project("restart")
    workflow_id, handle = await start_mcad_workflow(
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,
        kind="generate",
        idempotency_key=f"restart-{project_id}",
        objective="Worker 崩溃后恢复",
        primary=_box_execution(delay_seconds=8),
    )
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "TEMPORAL_TARGET": settings.temporal_target,
        "TEMPORAL_TASK_QUEUE": settings.temporal_task_queue,
        "TEMPORAL_AGENT_V2_TASK_QUEUE": settings.temporal_agent_v2_task_queue,
        "SANDBOX_RUNTIME": settings.sandbox_runtime,
        "SANDBOX_COMMAND": settings.sandbox_command,
        "SANDBOX_IMAGE": settings.sandbox_image,
        "DURABLE_CONTROL_PLANE_ENABLED": "true",
        "AUTH_REQUIRED": "false",
    }
    api_port = _available_port()
    api = await _start_api(env, api_port)
    first = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.workers.workflow_worker",
        cwd=str(ROOT),
        env=env,
    )
    second = None
    restarted_api = None
    try:
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await _snapshot(owner, workflow_id)
            if snapshot["attempts"] and snapshot["attempts"][-1]["status"] == "running":
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("first worker never started an execution attempt")
        first.send_signal(signal.SIGKILL)
        await first.wait()
        api.send_signal(signal.SIGKILL)
        await api.wait()

        second = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.workers.workflow_worker",
            cwd=str(ROOT),
            env=env,
        )
        await _wait_for_status(
            owner,
            workflow_id,
            {"waiting_confirmation"},
            timeout=45,
        )
        # Keep the API down while the worker recovers. Restarting it after
        # kernel execution also avoids an unrelated memory peak on small VMs.
        restarted_api = await _start_api(env, api_port)
        await confirm_mcad_workflow(workflow_id, accepted=True, note="恢复后确认")
        result = await asyncio.wait_for(handle.result(), timeout=45)
        assert result["status"] == "succeeded"
    finally:
        for process in (first, second, api, restarted_api):
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    snapshot = await _snapshot(owner, workflow_id)
    assert len(snapshot["attempts"]) == 2
    assert snapshot["attempts"][0]["status"] == "failed"
    assert snapshot["attempts"][0]["error_code"] == (
        "superseded_by_temporal_retry"
    )
    assert snapshot["attempts"][1]["status"] == "succeeded"
    assert len(snapshot["artifacts"]) == 2
    assert snapshot["change_set"]["status"] == "committed"
    assert (
        snapshot["change_set"]["head_revision_id"]
        == snapshot["change_set"]["candidate_revision_id"]
    )

class _EngineeringAcceptancePlanner(DurableAgentPlanner):
    """Fault injection only at requirement interpretation, never at measurement."""
    def __init__(self, diameter):
        super().__init__(planner=_NativeBudgetRequirements())
        self.diameter=diameter

    def compose_freecad_generation(self,*args,**kwargs):
        plan=super().compose_freecad_generation(*args,**kwargs)
        contract=plan.design_brief.acceptance
        checks=tuple(c.model_copy(update={'nominal':self.diameter}) if c.kind=='hole_diameter' else c for c in contract.checks)
        brief=plan.design_brief.model_copy(update={'acceptance':contract.model_copy(update={'checks':checks})})
        policy=plan.validation_policy.model_copy(update={'geometry':plan.validation_policy.geometry.model_copy(update={'repair_budget':0})})
        return plan.model_copy(update={'design_brief':brief,'validation_policy':policy})


class _RequirementsToolFixture:
    """Controlled provider replies through the real requirements parser/repair."""
    def __init__(self, objective):
        self.objective=objective
        self.calls=0

    async def create(self, **kwargs):
        from openai.types.chat import ChatCompletion
        self.calls+=1
        assert kwargs['tools'][0]['function']['name']=='submit_cad_plan'
        value=await _NativeBudgetRequirements().plan_new(
            [{'role':'user','content':self.objective}],require_acceptance=True)
        payload=value.model_dump(mode='json')
        if self.calls==1:
            payload['design_brief']['acceptance']['checks'][3]['scope']['axis']=None
        else:
            assert kwargs['messages'][-1]['tool_call_id']=='requirements-1'
            assert 'hole positions' in kwargs['messages'][-1]['content']
        return ChatCompletion(id=f'requirements-{self.calls}',model='controlled-requirements',created=1,
            object='chat.completion',choices=[{'index':0,'finish_reason':'tool_calls','message':{
                'role':'assistant','tool_calls':[{'id':f'requirements-{self.calls}','type':'function',
                    'function':{'name':'submit_cad_plan','arguments':json.dumps(payload)}}]}}])


@pytest.mark.asyncio(loop_scope='module')
async def test_unsupported_required_measurement_is_not_a_user_confirmation_or_cad_success():
    from app.services.workflow_admission import create_document_workflow
    class UnsupportedMeasurement(DurableAgentPlanner):
        async def requirements_generation(self, objective, **kwargs):
            value=await _NativeBudgetRequirements().plan_new(
                [{'role':'user','content':objective}],require_acceptance=True)
            acceptance=value.design_brief.acceptance.model_copy(update={
                'verification_limits':('Controlled unsupported required measurement',)})
            return value.model_copy(update={'design_brief':value.design_brief.model_copy(update={
                'acceptance':acceptance,'open_questions':[]})})
    owner,project_id,initial=await _seed_project('verification-unavailable')
    objective='Create a part with a required unsupported measurement.'
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        created=await create_document_workflow(conn,tenant_id=owner.tenant_id,project_id=project_id,
            requested_by_principal_id=owner.principal_id,kind='mcad.agent.v2.generate',
            idempotency_key=f'unsupported-{project_id}',request_payload={'objective':objective})
    request=McadAgentWorkflowV2Request(workflow_run_id=created.workflow_id,tenant_id=owner.tenant_id,
        project_id=project_id,principal_id=owner.principal_id,branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,operation='generate',modeling_backend='freecad',objective=objective)
    client=await get_temporal_client()
    async with build_agent_v2_workflow_worker(client,backend=get_execution_backend(),durable_planner=UnsupportedMeasurement()):
        handle=await client.start_workflow('McadAgentWorkflowV2',request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),task_queue=settings.temporal_agent_v2_task_queue)
        with pytest.raises(WorkflowFailureError) as error:
            await handle.result()
        from app.workflows.agent_v2 import McadAgentWorkflowV2
        cause=McadAgentWorkflowV2._root_application_error(error.value)
        assert cause is not None and cause.type=='engineering_verification_unavailable'
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        assert await conn.scalar(text('SELECT count(*) FROM execution_attempts WHERE workflow_run_id=:id'),{'id':created.workflow_id})==0
        assert await conn.scalar(text('SELECT count(*) FROM change_sets WHERE source_workflow_run_id=:id'),{'id':created.workflow_id})==0
        status=await conn.scalar(text('SELECT status FROM workflow_runs WHERE id=:id'),{'id':created.workflow_id})
        assert status=='failed'
        assert await conn.scalar(text('SELECT error_code FROM workflow_runs WHERE id=:id'),{'id':created.workflow_id})=='engineering_verification_unavailable'


class _NativeAPIProgramFixture:
    """Fixed native program as input; no execution or validation output is faked."""
    async def generate(self,**kwargs):
        from app.freecad.contracts import FreeCADOperationPlan
        from app.freecad.operation_generator import FreeCADOperationGenerationResult
        plan=FreeCADOperationPlan.model_validate({'operations':[
            {'op_id':'native-program','action':'api.execute','args':{'source':
                "base=document.addObject('Part::Box','Base')\nbase.Length=60;base.Width=40;base.Height=8\n"
                "tool=document.addObject('Part::Cylinder','Tool');tool.Radius=3;tool.Height=8\n"
                "tool.Placement.Base=App.Vector(30,20,0)\n"
                "cut=document.addObject('Part::Cut','Final');cut.Base=base;cut.Tool=tool\n"}},
            {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step','stl'],'objects':['Final']}}]})
        source=plan.model_dump_json()
        return FreeCADOperationGenerationResult(operation_plan=plan,source_code=source,
            generator_kind='controlled_native_api_program',provenance={**_controlled_provenance(),
                'provider':'controlled-native-program','response_hash':hashlib.sha256(source.encode()).hexdigest()})


def _native_tool_request(plan):
    """Serialize controlled native input through the public API tool envelope."""
    assert [op['action'] for op in plan['operations']] == ['api.execute', 'document.export']
    return {**{key: value for key, value in plan.items() if key not in {'operations', 'schema_version'}},
        'execute': {key: value for key, value in plan['operations'][0].items() if key != 'action'},
        'export': {key: value for key, value in plan['operations'][1].items() if key != 'action'}}


class _CurvedPanelToolFixture:
    """Real tool dispatch and native surface construction, controlled LLM input."""
    def __init__(self):
        from types import SimpleNamespace
        from app.freecad.operation_generator import FreeCADOperationGenerator
        self.calls=0
        self.engine=FreeCADOperationGenerator(client=SimpleNamespace(chat=SimpleNamespace(completions=self)),
            provenance_reader=lambda:{**_controlled_provenance(),'finish_reason':'tool_calls'})

    async def create(self, **kwargs):
        from openai.types.chat import ChatCompletion
        self.calls+=1
        if self.calls==1:
            name,args='freecad_discover',{'module':'Part','symbol':'Shape.makeOffsetShape'}
        else:
            name='freecad_execute_api'
            args={'operations':[
                {'op_id':'curved-panel','action':'api.execute','args':{'source':
                    "surface=Part.BezierSurface()\nsurface.increase(2,2)\n"
                    "for i,x in enumerate([-10,0,10],1):\n"
                    " for j,y in enumerate([-10,0,10],1):\n"
                    "  surface.setPole(i,j,App.Vector(x,y,[2,-2,2][i-1]+[2,-2,2][j-1]))\n"
                    "panel=document.addObject('PartDesign::Feature','Panel')\n"
                    "panel.Shape=surface.toShape().makeOffsetShape(-2,1e-7,fill=True)\n"
                    + ("panel.Placement.Rotation=App.Rotation(App.Vector(0,0,1),30)\n" if self.calls>2 else '')}},
                {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step','stl'],'objects':['Panel']}}]}
            args = _native_tool_request(args)
        return ChatCompletion(id=f'curve-{self.calls}',model='controlled-curved-panel',created=1,
            object='chat.completion',choices=[{'index':0,'finish_reason':'tool_calls','message':{
                'role':'assistant','tool_calls':[{'id':f'curve-{self.calls}','type':'function',
                    'function':{'name':name,'arguments':json.dumps(args)}}]}}])

    async def generate(self, **kwargs):
        return await self.engine._complete(user_payload={'task':'generate'},generator_kind='controlled_curve_tool',
            output_formats=kwargs['output_formats'],rejection_sink=kwargs.get('rejection_sink'))

    async def repair(self, **kwargs):
        # Rotate the real panel about the build axis. This changes the candidate
        # without fixing its overhangs; repeated inspection must retain failure.
        return await self.engine.repair(**kwargs)


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('required_thickness,dfm_mode',[(2,'advisory'),(2,'required'),(3,'advisory')])
async def test_curved_panel_tool_requires_whole_material_thickness_before_commit(required_thickness,dfm_mode):
    from app.services.workflow_admission import create_document_workflow
    from app.models.schemas import CADPlan,DesignBrief
    from app.contracts.acceptance import AcceptanceContract
    class CurveRequirements(DurableAgentPlanner):
        async def requirements_generation(self,objective,**kwargs):
            criteria=AcceptanceContract.model_validate({'objective':objective,'checks':[
                {'check_id':'whole-wall','kind':'wall_thickness','nominal':required_thickness,
                 'description':'whole panel normal thickness','source_quote':objective},
                {'check_id':'single-solid','kind':'solid_count','nominal':1,
                 'description':'one part','source_quote':objective}]})
            return CADPlan(description=objective,part_type='custom',dimensions={},features=['curved panel'],
                design_brief=DesignBrief(acceptance=criteria,open_questions=['必须确认：受控曲面验收测试']))
        async def decompose_generation(self,plan):
            return None
        def compose_freecad_generation(self,*args,**kwargs):
            from app.agent.durable_plan import GateMode
            plan=super().compose_freecad_generation(*args,**kwargs)
            # Exercise both advisory and required manufacturing gates with the
            # same geometry. Neither mode may relabel the real FDM result.
            policy=plan.validation_policy.model_copy(update={
                'geometry':plan.validation_policy.geometry.model_copy(update={'repair_budget':0}),
                'dfm':plan.validation_policy.dfm.model_copy(update={'mode':GateMode(dfm_mode),'repair_budget':0})})
            return plan.model_copy(update={'validation_policy':policy})
    owner,project_id,initial=await _seed_project('curved-panel-acceptance')
    objective=f'Create one freeform curved panel with {required_thickness} mm whole-part normal wall thickness.'
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        created=await create_document_workflow(conn,tenant_id=owner.tenant_id,project_id=project_id,
            requested_by_principal_id=owner.principal_id,kind='mcad.agent.v2.generate',
            idempotency_key=f'curved-{project_id}',request_payload={'objective':objective})
    request=McadAgentWorkflowV2Request(workflow_run_id=created.workflow_id,tenant_id=owner.tenant_id,
        project_id=project_id,principal_id=owner.principal_id,branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,operation='generate',modeling_backend='freecad',objective=objective)
    client=await get_temporal_client();provider=_CurvedPanelToolFixture()
    async with build_agent_v2_workflow_worker(client,backend=get_execution_backend(),
            durable_planner=CurveRequirements(),durable_visual=_V2PassingVisualStub(),freecad_operations=provider):
        handle=await client.start_workflow('McadAgentWorkflowV2',request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),task_queue=settings.temporal_agent_v2_task_queue)
        await _wait_for_status(owner,created.workflow_id,{'waiting_confirmation'})
        await confirm_mcad_workflow(created.workflow_id,accepted=True,note='Independent full panel measurement',workflow_kind='mcad.agent.v2.generate')
        can_commit=required_thickness==2 and dfm_mode=='advisory'
        if can_commit:
            result=await handle.result()
            assert result['status']=='succeeded'
        else:
            with pytest.raises(WorkflowFailureError) as failure:
                await handle.result()
            assert failure.value.cause.type==('agent_dfm_validation_failed' if required_thickness==2 else 'agent_geometry_validation_failed')
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        report=await conn.scalar(text("SELECT evidence FROM agent_validation_evidence WHERE workflow_run_id=:id AND gate='geometry'"),{'id':created.workflow_id})
        wall=next(e for e in report['acceptance']['evidence'] if e['check_id']=='whole-wall')
        assert wall['method']=='whole_part_certified_normal_layer_coverage'
        assert all(abs(v-2)<1e-7 for v in wall['measured'])
        assert wall['outcome']==('passed' if required_thickness==2 else 'failed')
        assert await conn.scalar(text('SELECT head_revision_id FROM project_branches WHERE id=:id'),{'id':initial.branch_id})==initial.revision_id
        if required_thickness==2:
            reports=(await conn.execute(text("SELECT mode,outcome,evidence FROM agent_validation_evidence WHERE workflow_run_id=:id AND gate='dfm'"),{'id':created.workflow_id})).mappings().all()
            assert len(reports)==2  # Real initial inspection and ineffective repair.
            assert all(dfm['mode']==dfm_mode and dfm['outcome']=='failed' for dfm in reports)
            assert all(any(v['rule_id']=='fdm_overhang' for v in dfm['evidence']['violations']) for dfm in reports)
        if not can_commit:
            assert await conn.scalar(text('SELECT count(*) FROM change_sets WHERE source_workflow_run_id=:id'),{'id':created.workflow_id})==0
    if can_commit:
        from app.services.change_sets import ValidationRequired
        change_set=UUID(result['change_set_id'])
        with pytest.raises(ValidationRequired,match='风险审查意见'):
            await accept_change_set(tenant_id=owner.tenant_id,reviewer_principal_id=owner.principal_id,change_set_id=change_set)
        await accept_change_set(tenant_id=owner.tenant_id,reviewer_principal_id=owner.principal_id,
            change_set_id=change_set,review_note='仅保存几何设计；已知悬垂需要支撑，尚不批准直接制造。')
        committed=await commit_change_set(tenant_id=owner.tenant_id,reviewer_principal_id=owner.principal_id,change_set_id=change_set)
        assert committed.status=='committed'
    assert provider.calls==(3 if required_thickness==2 else 2)


class _NativeToolProgramFixture:
    """Scripted provider calls; production tool protocol, execution and gates."""
    def __init__(self, *, fail_first=False):
        from types import SimpleNamespace
        from app.freecad.operation_generator import FreeCADOperationGenerator
        self.calls = 0
        self.fail_first = fail_first
        self.engine = FreeCADOperationGenerator(client=SimpleNamespace(chat=SimpleNamespace(completions=self)),
            provenance_reader=lambda: {**_controlled_provenance(), 'finish_reason':'tool_calls'})

    async def create(self, **kwargs):
        from openai.types.chat import ChatCompletion
        self.calls += 1
        assert kwargs['tools'] and kwargs['tool_choice']=='auto'
        if self.calls==1:
            name,args='freecad_discover',{'module':'Part','symbol':'makeBox'}
        else:
            generated=await _NativeAPIProgramFixture().generate()
            args=json.loads(generated.source_code)
            if self.calls==2 and self.fail_first:
                args['operations'][0]['args']['source']="raise RuntimeError('controlled native tool failure')"
            name='freecad_execute_api'
            args = _native_tool_request(args)
        return ChatCompletion(id=f'controlled-tool-{self.calls}',model='controlled-tools',created=1,
            object='chat.completion',choices=[{'index':0,'finish_reason':'tool_calls','message':{
                'role':'assistant','tool_calls':[{'id':f'call-{self.calls}','type':'function',
                    'function':{'name':name,'arguments':json.dumps(args)}}]}}])

    async def generate(self, **kwargs):
        return await self.engine._complete(user_payload={'task':'generate'},generator_kind='controlled_tool_protocol',
            output_formats=kwargs['output_formats'],rejection_sink=kwargs.get('rejection_sink'))

    async def repair(self, **kwargs):
        return await self.engine.repair(**kwargs)


class _LiveToolProvider:
    """Exercise the paid model tool path, bypassing only the deterministic compiler.

    Requirement/vision fixtures are explicit; CAD execution and final STEP
    acceptance remain real. This isolates tools, not end-user success rates.
    """
    def __init__(self):
        from app.freecad.operation_generator import FreeCADOperationGenerator
        self.engine = FreeCADOperationGenerator()

    async def generate(self, **kwargs):
        plan=kwargs['plan']
        return await self.engine._complete(user_payload={'task':'generate',
            'agent_plan':{'objective':plan.objective}, 'requirements':kwargs['requirements'],
            'instruction':'Before building, use freecad_describe_operation to check the exact schema of a modeling operation you will use.',
            'required_export_formats':['fcstd',*kwargs['output_formats']]},
            generator_kind='live_freecad_tools', output_formats=kwargs['output_formats'],
            rejection_sink=kwargs.get('rejection_sink'))

    async def repair(self, **kwargs):
        return await self.engine.repair(**kwargs)


class _CheckpointToolFixture(_NativeToolProgramFixture):
    """Fixed tool inputs; checkpoints, restart reads, kernel and measurements are real."""
    async def create(self, **kwargs):
        from openai.types.chat import ChatCompletion
        self.calls += 1
        payload = json.loads(kwargs['messages'][1]['content'])
        state = payload.get('base_freecad_state')
        replies = [m for m in kwargs['messages'] if m['role']=='tool']
        if state and not replies:
            name, args = 'freecad_inspect', {'objects':['Base'], 'fields':['properties','geometry']}
        else:
            name = 'freecad_execute_api'
            if not state:
                program = "base=document.addObject('Part::Box','Base')\nbase.Length=60;base.Width=40;base.Height=8"
                mode, roots, formats = 'checkpoint', ['Base'], ['fcstd']
            else:
                assert 'Base' in json.dumps(state)
                if payload.get('task')!='repair':
                    feedback=payload['execution_feedback']
                    assert feedback['status']=='executed' and feedback['saved_revision'] is None
                program = ("base=document.getObject('Base')\n"
                    "assert base is not None and abs(base.Shape.Volume-19200)<1e-6\n"
                    "tool=document.addObject('Part::Cylinder','Tool');tool.Radius=3;tool.Height=8\n"
                    "tool.Placement.Base=App.Vector(30,20,0)\n"
                    "cut=document.addObject('Part::Cut','Final');cut.Base=base;cut.Tool=tool")
                if self.fail_first and payload.get('task')!='repair':
                    # The failed attempt creates an object before failing. The
                    # repair must load the clean checkpoint, with no leaked Tool.
                    program += "\nraise RuntimeError('controlled incremental failure')"
                mode, roots, formats = 'final', ['Final'], ['fcstd','step','stl']
            args={'execution_mode':mode,'operations':[
                {'op_id':'program','action':'api.execute','args':{'source':program}},
                {'op_id':'export','action':'document.export','args':{'objects':roots,'formats':formats}}]}
            args = _native_tool_request(args)
        return ChatCompletion(id=f'checkpoint-{self.calls}',model='checkpoint-fixture',created=1,
            object='chat.completion',choices=[{'index':0,'finish_reason':'tool_calls','message':{
                'role':'assistant','tool_calls':[{'id':f'checkpoint-call-{self.calls}','type':'function',
                    'function':{'name':name,'arguments':json.dumps(args)}}]}}])

    async def generate(self, **kwargs):
        if kwargs.get('execution_feedback'):
            return await self.engine.generate(**kwargs)
        return await self.engine._complete(user_payload={'task':'generate','checkpoint_enabled':kwargs['checkpoint_enabled']},
            generator_kind='checkpoint-fixture', output_formats=kwargs['output_formats'])


class _LiveCheckpointProvider(_LiveToolProvider):
    """Paid tool-loop probe with frozen requirements, real intermediate CAD state."""
    async def generate(self, **kwargs):
        if kwargs.get('execution_feedback'):
            return await self.engine.generate(**kwargs)
        return await self.engine._complete(user_payload={
            'task':'generate','checkpoint_enabled':True,
            'agent_plan':{'objective':kwargs['plan'].objective},
            'requirements':kwargs['requirements'],
            'instruction':'Use freecad_describe_operation for a needed schema first. For this tool protocol contract, '
                'execute the plate stock as execution_mode=checkpoint, exporting fcstd; leave the hole for the '
                'next turn after the actual checkpoint is returned. Do not claim completion. The next turn '
                'must inspect that verified state, add the requested centered through hole, and export the final formats.',
            'required_export_formats':['fcstd',*kwargs['output_formats']]},
            generator_kind='live_freecad_checkpoint', output_formats=kwargs['output_formats'],
            rejection_sink=kwargs.get('rejection_sink'))


class _RestartCheckpointFixture(_CheckpointToolFixture):
    def __init__(self):
        super().__init__()
        self.entered=asyncio.Event()
        self.release=asyncio.Event()
    async def generate(self, **kwargs):
        if kwargs.get('execution_feedback') and not self.release.is_set():
            self.entered.set()
            await self.release.wait()
        return await super().generate(**kwargs)


class _SlowCheckpointFixture(_CheckpointToolFixture):
    async def create(self, **kwargs):
        response=await super().create(**kwargs)
        call=response.choices[0].message.tool_calls[0]
        if call.function.name=='freecad_execute_api':
            args=json.loads(call.function.arguments)
            if args['execution_mode']=='final':
                args['execute']['args']['source']='import time\ntime.sleep(5)\n'+args['execute']['args']['source']
                call.function.arguments=json.dumps(args)
        return response


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('interruption',['generation','execution','cancel'])
async def test_freecad_checkpoint_survives_worker_restart_without_rebuilding_base(interruption,monkeypatch):
    from app.services.workflow_admission import create_document_workflow
    owner,project_id,initial=await _seed_project('checkpoint-restart')
    objective='Create a 60x40x8 mm plate with one centered 6 mm through hole.'
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        created=await create_document_workflow(conn,tenant_id=owner.tenant_id,project_id=project_id,
            requested_by_principal_id=owner.principal_id,kind='mcad.agent.v2.generate',
            idempotency_key=f'checkpoint-restart-{project_id}',request_payload={'objective':objective})
    request=McadAgentWorkflowV2Request(workflow_run_id=created.workflow_id,tenant_id=owner.tenant_id,
        project_id=project_id,principal_id=owner.principal_id,branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,operation='generate',modeling_backend='freecad',objective=objective)
    client=await get_temporal_client()
    generator=_RestartCheckpointFixture() if interruption=='generation' else _SlowCheckpointFixture()
    backend=get_execution_backend()
    # A configured legacy one-second fallback must not kill the new leased CAD
    # job, including its deliberately slower real FreeCAD program.
    monkeypatch.setattr(settings,'sandbox_timeout_s',1)
    original_execute=backend.execute
    native_specs=[]
    async def observe(spec,**kwargs):
        assert spec.limits.timeout_seconds is None
        if spec.capability=='mcad.freecad':native_specs.append(spec)
        return await original_execute(spec,**kwargs)
    monkeypatch.setattr(backend,'execute',observe)
    worker_args=dict(backend=backend,durable_planner=_EngineeringAcceptancePlanner(6),
                     durable_visual=_V2PassingVisualStub(),freecad_operations=generator)
    async with build_agent_v2_workflow_worker(client,**worker_args):
        handle=await client.start_workflow('McadAgentWorkflowV2',request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),task_queue=settings.temporal_agent_v2_task_queue)
        await _wait_for_status(owner,created.workflow_id,{'waiting_confirmation'})
        await confirm_mcad_workflow(created.workflow_id,accepted=True,note='Checkpoint restart contract',workflow_kind='mcad.agent.v2.generate')
        if interruption=='generation':
            await generator.entered.wait()
        else:
            # Observe the real Docker container, not merely a queued activity.
            while len(native_specs)<2 or not backend._sandbox._active_containers:
                await asyncio.sleep(0.05)
        async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
            before=(await conn.execute(text('SELECT id,manifest_hash FROM agent_staging_manifests WHERE workflow_run_id=:id'),{'id':created.workflow_id})).mappings().one()
        assert generator.calls==(1 if interruption=='generation' else 3)
        if interruption=='cancel':
            await cancel_mcad_workflow(tenant_id=owner.tenant_id,principal_id=owner.principal_id,
                workflow_run_id=created.workflow_id,reason='Cancel active native checkpoint continuation')
            result=await handle.result()
            assert result['status']=='cancelled'
            while backend._sandbox._active_containers:
                await asyncio.sleep(0.05)
    assert not backend._sandbox._active_containers
    if interruption=='cancel':
        async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
            assert await conn.scalar(text('SELECT count(*) FROM agent_staging_manifests WHERE workflow_run_id=:id'),{'id':created.workflow_id})==1
            assert await conn.scalar(text('SELECT count(*) FROM change_sets WHERE source_workflow_run_id=:id'),{'id':created.workflow_id})==0
        return
    # A new worker must consume the persisted native checkpoint. The provider
    # has never emitted the second transaction and no in-memory document exists.
    if interruption=='generation':generator.release.set()
    async with build_agent_v2_workflow_worker(client,**worker_args):
        result=await handle.result()
        assert result['status']=='succeeded'
    assert generator.calls==3  # one first execute, then inspect and final execute
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        after=(await conn.execute(text('SELECT id,manifest_hash FROM agent_staging_manifests WHERE workflow_run_id=:id ORDER BY created_at'),{'id':created.workflow_id})).mappings().all()
        assert len(after)==2 and dict(after[0])==dict(before)
        assert await conn.scalar(text("SELECT count(*) FROM execution_attempts WHERE workflow_run_id=:id AND status='succeeded' AND result_payload->>'source_id'=(SELECT manifest->>'source_id' FROM agent_staging_manifests WHERE id=:manifest)"),{'id':created.workflow_id,'manifest':before['id']})==1


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.skipif(not REAL_FREECAD_AGENT, reason='paid FreeCAD tool provider explicitly enabled')
async def test_agent_v2_live_freecad_tools_contract():
    await test_native_engineering_acceptance_gates_commit(6, 'live_tools')


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.skipif(not REAL_FREECAD_AGENT, reason='paid checkpoint provider explicitly enabled')
async def test_agent_v2_live_freecad_checkpoint_contract():
    await test_native_engineering_acceptance_gates_commit(6, 'live_checkpoints')


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('required_diameter',[6,7])
@pytest.mark.parametrize('execution_path',['typed','api','tools','tool_repair','checkpoint','checkpoint_repair','requirements_tool'])
async def test_native_engineering_acceptance_gates_commit(required_diameter,execution_path,output_formats=('step','stl')):
    """Real Temporal→FreeCAD→STEP→S3→DB→seal/commit, controlled requirements/vision.

    The negative deliberately plans a 6 mm hole while the frozen criterion asks
    for 7 mm. A correct volume/bounds/closed-solid check must not authorize it.
    """
    from app.services.workflow_admission import create_document_workflow
    owner,project_id,initial=await _seed_project('engineering-acceptance')
    objective=f'Create a 60x40x8 mm plate with one centered {required_diameter} mm through hole.'
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        created=await create_document_workflow(conn,tenant_id=owner.tenant_id,project_id=project_id,
            requested_by_principal_id=owner.principal_id,kind='mcad.agent.v2.generate',
            idempotency_key=f'engineering-{project_id}',request_payload={'objective':objective,
                'branch_id':str(initial.branch_id),'expected_base_revision_id':str(initial.revision_id),
                'output_formats':list(output_formats)})
    request=McadAgentWorkflowV2Request(workflow_run_id=created.workflow_id,tenant_id=owner.tenant_id,
        project_id=project_id,principal_id=owner.principal_id,branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id,operation='generate',modeling_backend='freecad',objective=objective,
        output_formats=output_formats)
    client=await get_temporal_client()
    operation_generator = (_LiveCheckpointProvider() if execution_path=='live_checkpoints' else
        _LiveToolProvider() if execution_path=='live_tools' else
        _CheckpointToolFixture(fail_first=execution_path=='checkpoint_repair') if execution_path.startswith('checkpoint') else
        _NativeAPIProgramFixture() if execution_path=='api' else
        _NativeToolProgramFixture(fail_first=execution_path=='tool_repair') if execution_path in {'tools','tool_repair'} else None)
    durable_planner=_EngineeringAcceptancePlanner(required_diameter)
    requirements_provider=None
    if execution_path=='requirements_tool':
        from types import SimpleNamespace
        from app.agent.planner import Planner
        requirements_provider=_RequirementsToolFixture(objective)
        durable_planner.planner=Planner()
        durable_planner.planner._client=SimpleNamespace(chat=SimpleNamespace(completions=requirements_provider))
    async with build_agent_v2_workflow_worker(client,backend=get_execution_backend(),
            durable_planner=durable_planner,durable_visual=_V2PassingVisualStub(),
            **({'freecad_operations':operation_generator} if operation_generator else {})):
        handle=await client.start_workflow('McadAgentWorkflowV2',request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id),task_queue=settings.temporal_agent_v2_task_queue)
        await _wait_for_status(owner,created.workflow_id,{'waiting_confirmation'})
        await confirm_mcad_workflow(created.workflow_id,accepted=True,note='Deterministic measurement contract test',workflow_kind='mcad.agent.v2.generate')
        if required_diameter==6:
            result=await handle.result()
            assert result['status']=='succeeded'
        else:
            with pytest.raises(WorkflowFailureError) as error:
                await handle.result()
            assert error.value.cause.type=='agent_geometry_validation_failed'
    if requirements_provider:
        assert requirements_provider.calls==2
    async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
        rows=(await conn.execute(text("SELECT id,outcome,evidence FROM agent_validation_evidence WHERE workflow_run_id=:id AND gate='geometry'"),{'id':created.workflow_id})).mappings().all()
        assert len(rows)==1
        report=rows[0]['evidence'];assert report['request_sha256']
        measured={e['check_id']:e for e in report['acceptance']['evidence']}
        assert measured['hole-diameter']['measured']==[6]
        criteria={c['check_id']:c for c in report['acceptance_contract']['checks']}
        assert criteria['hole-diameter']['nominal']==required_diameter
        if output_formats==('stl',):
            manifest=await conn.scalar(text('SELECT manifest FROM agent_staging_manifests WHERE workflow_run_id=:id'),{'id':created.workflow_id})
            assert 'step' not in {a['format'] for a in manifest['outputs']}
            assert [a['format'] for a in manifest['verification_outputs']]==['step']
            assert any(a['sha256']==manifest['verification_outputs'][0]['sha256'] for a in report['artifacts'])
        if execution_path=='live_checkpoints':
            staged=(await conn.execute(text('SELECT manifest FROM agent_staging_manifests WHERE workflow_run_id=:id ORDER BY created_at'),{'id':created.workflow_id})).scalars().all()
            assert len(staged)>=2
            assert staged[-1]['execution_mode']=='final'
            assert any(s['execution_mode']=='checkpoint' for s in staged[:-1])
            dispatched=(await conn.execute(text("SELECT payload FROM task_events WHERE workflow_run_id=:id AND event_type='agent.freecad.tool_dispatched' ORDER BY sequence"),{'id':created.workflow_id})).scalars().all()
            assert len(dispatched)>=len(staged) and all(d['provider_response_id'] for d in dispatched)
            assert await conn.scalar(text("SELECT count(*) FROM task_events WHERE workflow_run_id=:id AND event_type='agent.freecad.tool_read' AND payload->'call'->'function'->>'name'='freecad_inspect'"),{'id':created.workflow_id})>0
        if execution_path.startswith('checkpoint'):
            from app.workflows.checkpoint_inputs import checkpoint_context
            from temporalio.exceptions import ApplicationError
            stages=(await conn.execute(text('SELECT id, candidate_build_id, manifest FROM agent_staging_manifests WHERE workflow_run_id=:id ORDER BY created_at'),{'id':created.workflow_id})).mappings().all()
            assert len(stages)==2
            first, final = stages
            assert first['manifest']['execution_mode']=='checkpoint'
            assert final['manifest']['execution_mode']=='final'
            assert final['manifest']['predecessor_checkpoint_id']==str(first['id'])
            assert {a['format'] for a in first['manifest']['outputs']}=={'fcstd','state','capability-result'}
            state, feedback=await checkpoint_context(request,first['candidate_build_id'],first['id'])
            assert any(o['name']=='Base' for o in state['objects'])
            assert not any(o['name'] in {'Tool','Final'} for o in state['objects'])
            assert feedback['engineering_validation']=='pending'
            with pytest.raises(ApplicationError, match='this workflow and candidate'):
                await checkpoint_context(request,uuid4(),first['id'])
            with pytest.raises(ApplicationError, match='execution mode'):
                await checkpoint_context(request,final['candidate_build_id'],final['id'])
            if execution_path=='checkpoint_repair':
                assert await conn.scalar(text("SELECT count(*) FROM execution_attempts WHERE workflow_run_id=:id AND status='failed'"),{'id':created.workflow_id})==1
        if execution_path in {'tools','tool_repair','live_tools'}:
            dispatched=(await conn.execute(text("SELECT payload FROM task_events WHERE workflow_run_id=:id AND event_type='agent.freecad.tool_dispatched' ORDER BY sequence"),{'id':created.workflow_id})).scalars().all()
            if execution_path=='live_tools':
                assert dispatched and all(d['provider_response_id'] for d in dispatched)
                assert await conn.scalar(text("SELECT count(*) FROM task_events WHERE workflow_run_id=:id AND event_type='agent.freecad.tool_read'"),{'id':created.workflow_id}) > 0
            else:
                assert len(dispatched)==(2 if execution_path=='tool_repair' else 1)
            staged=(await conn.execute(text('SELECT a.result_payload FROM agent_staging_manifests m JOIN execution_attempts a ON a.id=m.execution_attempt_id WHERE m.workflow_run_id=:id'),{'id':created.workflow_id})).scalars().all()
            assert len(staged)==1
            receipt=staged[0]['tool_result']
            assert receipt['status']=='executed' and receipt['saved_revision'] is None
            assert receipt['engineering_validation']=='pending'
            assert receipt['source_hash']==dispatched[-1]['source_hash']
            assert receipt['source_id']==dispatched[-1]['source_id']
            if execution_path!='live_tools':
                assert operation_generator.calls==(3 if execution_path=='tool_repair' else 2)
        if required_diameter==7:
            assert rows[0]['outcome']=='failed' and measured['hole-diameter']['outcome']=='failed'
            assert await conn.scalar(text('SELECT count(*) FROM change_sets WHERE source_workflow_run_id=:id'),{'id':created.workflow_id})==0
            assert await conn.scalar(text('SELECT head_revision_id FROM project_branches WHERE id=:id'),{'id':initial.branch_id})==initial.revision_id
    from app.services.task_evidence import get_validation_evidence
    public=await get_validation_evidence(owner,created.workflow_id,rows[0]['id'])
    assert public['report']['acceptance']==report['acceptance']
    assert public['report']['acceptance_contract']==report['acceptance_contract']
    assert public['report']['request_sha256']==report['request_sha256']
    if required_diameter==6:
        change_set=UUID(result['change_set_id'])
        await accept_change_set(tenant_id=owner.tenant_id,reviewer_principal_id=owner.principal_id,change_set_id=change_set)
        committed=await commit_change_set(tenant_id=owner.tenant_id,reviewer_principal_id=owner.principal_id,change_set_id=change_set)
        assert committed.status=='committed'
        if output_formats==('stl',):
            async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
                exports=(await conn.execute(text('SELECT artifact_kind FROM artifacts WHERE workflow_run_id=:id'),{'id':created.workflow_id})).scalars().all()
                assert 'stl' in exports and 'step' not in exports and 'verification_step' not in exports
        if execution_path=='tools':
            from functools import partial
            from temporalio.testing import ActivityEnvironment
            from app.workflows.handlers.native_generation import agent_generate_operations
            from app.workflows.handlers.cad_execution import agent_execute_freecad
            from app.freecad.repair_validation import NativeConstraintRepairValidation
            async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
                job_payload=await conn.scalar(text("SELECT payload FROM model_jobs WHERE workflow_run_id=:id AND operation='agent_v2.generate_operations'"),{'id':created.workflow_id})
            # Replay after commit must return persisted results, never call the
            # provider again or rerun CAD against the now-different branch head.
            replay=await agent_generate_operations(job_payload,freecad_operations=operation_generator)
            assert replay['replayed'] and operation_generator.calls==2
            replay_execution=await ActivityEnvironment().run(
                partial(agent_execute_freecad,backend=get_execution_backend(),
                    constraint_validation=NativeConstraintRepairValidation()),{**job_payload,**replay})
            assert replay_execution['replayed']
            assert replay_execution['tool_result']==receipt
        if execution_path in {'live_tools','live_checkpoints'} and os.environ.get('CAD_NATIVE_TOOL_REPORT'):
            async with tenant_transaction(owner.tenant_id,owner.principal_id) as conn:
                sources=(await conn.execute(text('SELECT provider, model, provider_response_id, request_hash, response_hash, finish_reason, usage, source_hash, source_code FROM agent_generated_sources WHERE workflow_run_id=:id ORDER BY created_at'),{'id':created.workflow_id})).mappings().all()
            Path(os.environ['CAD_NATIVE_TOOL_REPORT']).write_text(json.dumps({
                'status':'passed','workflow_id':str(created.workflow_id),'model_tool_provider':'live',
                'requirements_provider':'controlled_frozen_contract','vision_provider':'controlled_passing_fixture',
                'runtime':'real_FreeCAD_Temporal_PostgreSQL_S3','tool_calls':dispatched,
                'sources':[dict(row) for row in sources],'geometry':report,'commit_status':committed.status,
                'scope':'tool protocol integration only; not end-user model success rate',
                'execution_path':execution_path},ensure_ascii=False,indent=2))


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('required_diameter',[6,7])
async def test_native_stl_only_delivery_keeps_exact_acceptance(required_diameter):
    await test_native_engineering_acceptance_gates_commit(required_diameter,'typed',output_formats=('stl',))


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('connected_fixture', [True, False], ids=['valid-baseline', 'original-self-intersecting-baseline'])
async def test_constraint_patch_incident_full_chain_and_owned_evidence(connected_fixture):
    from tests.constraint_incident import IncidentRequirements, IncidentGenerator
    from app.services.workflow_admission import create_document_workflow
    from app.workflows.constraint_evidence import load_repair_contract, load_failure_snapshot, evidence_key
    from app.freecad.constraint_patch import verify_native_receipt
    owner, project_id, initial = await _seed_project('constraint-repair')
    objective = '设计一个可夹在 25 mm 桌板上的耳机挂钩，最终为一个连通实体'
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        created = await create_document_workflow(conn, tenant_id=owner.tenant_id, project_id=project_id,
            requested_by_principal_id=owner.principal_id, kind='mcad.agent.v2.generate',
            idempotency_key=f'constraint-{project_id}', request_payload={'objective': objective})
    request = McadAgentWorkflowV2Request(workflow_run_id=created.workflow_id, tenant_id=owner.tenant_id,
        project_id=project_id, principal_id=owner.principal_id, branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id, operation='generate', modeling_backend='freecad', objective=objective)
    generator = IncidentGenerator(live_repair=os.environ.get('CAD_CONSTRAINT_LIVE_REPAIR') == '1',
                                  connected_fixture=connected_fixture)
    client = await get_temporal_client()
    async with build_agent_v2_workflow_worker(client, backend=get_execution_backend(),
            durable_planner=IncidentRequirements(), freecad_operations=generator):
        handle = await client.start_workflow('McadAgentWorkflowV2', request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id), task_queue=settings.temporal_agent_v2_task_queue)
        # Complete trusted dimensions need no interactive requirements gate.
        if connected_fixture:
            result = await handle.result()
            assert result['status'] == 'succeeded'
        else:
            with pytest.raises(WorkflowFailureError) as error:
                await handle.result()
            from app.workflows.agent_v2 import McadAgentWorkflowV2
            cause = McadAgentWorkflowV2._root_application_error(error.value)
            assert cause is not None and cause.type == 'profile_replan_rejected'
            assert 'earlier constraint proof' in str(cause)
    if not generator.live_repair:
        assert generator.repairs == (4 if connected_fixture else 2)
    assert generator.generations == (3 if connected_fixture else 2)
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        events = (await conn.execute(text('SELECT event_type,payload FROM task_events WHERE workflow_run_id=:id ORDER BY sequence'),
            {'id': created.workflow_id})).mappings().all()
        sources = (await conn.execute(text('SELECT id,source_hash,source_code FROM agent_generated_sources WHERE workflow_run_id=:id ORDER BY created_at'),
            {'id': created.workflow_id})).mappings().all()
        manifests = (await conn.execute(text('SELECT manifest,execution_attempt_id FROM agent_staging_manifests WHERE workflow_run_id=:id ORDER BY created_at'),
            {'id': created.workflow_id})).mappings().all()
        failures = (await conn.execute(text("SELECT id FROM execution_attempts WHERE workflow_run_id=:id AND status='failed'"),
            {'id': created.workflow_id})).scalars().all()
        assert not set(failures) & {m['execution_attempt_id'] for m in manifests}
        assert len(manifests) == (3 if connected_fixture else 1)
        checks = (await conn.execute(text("SELECT evidence FROM agent_validation_evidence WHERE workflow_run_id=:id AND gate='geometry'"),
            {'id': created.workflow_id})).scalars().all()
        assert len(checks) == (1 if connected_fixture else 0)
        if connected_fixture:
            values = {item['check_id']: item for item in checks[0]['acceptance']['evidence']}
            assert values['gap']['outcome'] == values['one-solid']['outcome'] == 'passed'
        if not connected_fixture:
            assert await conn.scalar(text('SELECT count(*) FROM change_sets WHERE source_workflow_run_id=:id'),
                                     {'id': created.workflow_id}) == 0
            assert await conn.scalar(text('SELECT head_revision_id FROM project_branches WHERE id=:id'),
                                     {'id': initial.branch_id}) == initial.revision_id
    diagnostics = [e['payload'] for e in events if e['event_type'] == 'agent.freecad.constraint_diagnostic']
    assert len(diagnostics) == generator.repairs + (0 if connected_fixture else 1)
    for observed in diagnostics:
        failure = {'execution_attempt_id': observed['execution_attempt_id'],
                   'details': {'constraint_diagnostic_sha256': observed['sha256']}}
        evidence = await load_failure_snapshot(request, source_id=observed['source_id'],
            source_hash=observed['source_hash'], failure=failure)
        assert evidence['snapshot']['valid_checkpoint'] is False
        with pytest.raises(ValueError, match='owned'):
            await load_failure_snapshot(request.model_copy(update={'workflow_run_id': uuid4()}),
                source_id=observed['source_id'], source_hash=observed['source_hash'], failure=failure)
    rejected = [e['payload'] for e in events if e['event_type'] == 'agent.freecad.plan_rejected']
    if not generator.live_repair:
        assert len(rejected) == generator.repairs
        for record in rejected:
            details = json.loads(await get_object(record['evidence_object_key']))
            assert details['differences'][0]['field'] == 'sketch'
            assert details['differences'][0]['after'] == 'UnrelatedSketch'
    final = sources[-1]
    certificate = await load_repair_contract(request, source_id=final['id'], source_hash=final['source_hash'])
    assert len(certificate['patches']) == generator.repairs
    native = None
    if connected_fixture:
        from app.freecad.failure_snapshot import digest
        verified = next(e['payload'] for e in events if e['event_type']=='agent.freecad.constraint_verified'
                        and e['payload']['source_id']==str(final['id']))
        saved = json.loads(await get_object(evidence_key(request, verified['sha256'])))
        assert saved['execution_attempt_id'] == str(manifests[-1]['execution_attempt_id'])
        assert saved['source_id'] == str(final['id'])
        native = saved['report']
        assert native['status'] == 'passed' and native['contract_hash'] == digest(certificate)
        assert native['parameter_probes'] and all(p['downstream_recomputed'] for p in native['parameter_probes'])
        change_set = UUID(result['change_set_id'])
        await accept_change_set(tenant_id=owner.tenant_id, reviewer_principal_id=owner.principal_id, change_set_id=change_set,
            review_note='本测试只确认原生尺寸、约束、联动和实体检查；知悉外观参考检查未验证')
        committed = await commit_change_set(tenant_id=owner.tenant_id, reviewer_principal_id=owner.principal_id, change_set_id=change_set)
        assert committed.status == 'committed'
        # Check the artifact owned by the saved branch head, in a new restricted
        # FreeCAD process. The original failed incident and its criteria remain
        # unchanged; only the separate valid frozen baseline reaches this point.
        async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
            artifact = (await conn.execute(text("SELECT a.* FROM artifacts a JOIN project_branches b ON b.head_revision_id=a.revision_id WHERE b.id=:id AND a.artifact_kind='fcstd'"),
                {'id': initial.branch_id})).mappings().one()
        payload = await get_object(artifact['object_key'])
        assert hashlib.sha256(payload).hexdigest() == artifact['sha256']
        import tempfile
        from tests.constraint_incident import acceptance
        with tempfile.TemporaryDirectory(prefix='constraint-saved-') as directory:
            Path(directory).chmod(0o755)
            Path(directory, 'model.FCStd').write_bytes(payload)
            Path(directory, 'acceptance.json').write_text(acceptance(objective).model_dump_json())
            Path(directory, 'verify.py').write_bytes((Path(__file__).resolve().parents[1]/'e2e/constraint_saved_measurements.py').read_bytes())
            process = await asyncio.create_subprocess_exec(settings.sandbox_command, 'run', '--rm',
                '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--tmpfs', '/tmp:rw,size=2g', '-v', f'{directory}:/sandbox/input:ro',
                '--entrypoint', '/opt/freecad/bin/FreeCADCmd', settings.sandbox_image,
                '-c', "exec(compile(open('/sandbox/input/verify.py').read(), '/sandbox/input/verify.py', 'exec'))",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            measured, _ = await process.communicate()
            assert process.returncode == 0 and b'CAD_CONSTRAINT_SAVED_MEASUREMENTS=' in measured and b'Traceback (most recent call last)' not in measured, measured.decode()
            if os.environ.get('CAD_AGENT_TEST_EVIDENCE_DIR'):
                retained = Path(os.environ['CAD_AGENT_TEST_EVIDENCE_DIR'])/str(created.workflow_id)
                retained.mkdir(parents=True, exist_ok=True)
                (retained/'saved.FCStd').write_bytes(payload)
                (retained/'saved-measurements.log').write_bytes(measured)
    if os.environ.get('CAD_CONSTRAINT_REPORT'):
        Path(os.environ['CAD_CONSTRAINT_REPORT']).with_suffix('.positive.json' if connected_fixture else '.negative.json').write_text(json.dumps({'status': 'passed',
            'workflow_id': str(created.workflow_id), 'repairs': generator.repairs, 'diagnostics': len(diagnostics),
            'rejections': len(rejected), 'requirements': checks[0] if checks else None, 'native_verification': native,
            'committed': connected_fixture, 'provider': 'live' if generator.live_repair else 'controlled_proposals',
            'scope': 'real Temporal, PostgreSQL, S3 and FreeCAD; DFM disabled, advisory visual availability recorded separately'}, ensure_ascii=False, indent=2))


@pytest.mark.asyncio(loop_scope='module')
@pytest.mark.parametrize('scenario', ['success', 'wrong-gap', 'budget'])
async def test_profile_replan_full_chain_preserves_acceptance_and_checkpoints(scenario):
    from tests.profile_replan_scenario import ProfileRequirements, ProfileGenerator
    from tests.e2e.profile_replan_fixture import acceptance
    from app.services.workflow_admission import create_document_workflow
    from app.workflows.constraint_evidence import load_profile_contract, evidence_key
    from app.services.task_diagnostics import failure_diagnostic
    from temporalio.worker import Replayer
    from app.workflows.agent_v2 import McadAgentWorkflowV2
    owner, project_id, initial = await _seed_project('profile-replan')
    objective = acceptance()['objective']
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        created = await create_document_workflow(conn, tenant_id=owner.tenant_id, project_id=project_id,
            requested_by_principal_id=owner.principal_id, kind='mcad.agent.v2.generate',
            idempotency_key=f'profile-{project_id}', request_payload={'objective': objective})
    request = McadAgentWorkflowV2Request(workflow_run_id=created.workflow_id, tenant_id=owner.tenant_id,
        project_id=project_id, principal_id=owner.principal_id, branch_id=initial.branch_id,
        expected_base_revision_id=initial.revision_id, operation='generate', modeling_backend='freecad', objective=objective)
    generator = ProfileGenerator(scenario)
    client = await get_temporal_client()
    async with build_agent_v2_workflow_worker(client, backend=get_execution_backend(),
            durable_planner=ProfileRequirements(), freecad_operations=generator):
        handle = await client.start_workflow('McadAgentWorkflowV2', request.temporal_payload(),
            id=temporal_agent_v2_workflow_id(created.workflow_id), task_queue=settings.temporal_agent_v2_task_queue)
        if scenario == 'success':
            result = await handle.result()
            assert result['status'] == 'succeeded'
        else:
            with pytest.raises(WorkflowFailureError) as failed:
                await handle.result()
            cause = McadAgentWorkflowV2._root_application_error(failed.value)
            assert cause.type == ('profile_geometry_invalid' if scenario == 'budget' else 'agent_geometry_validation_failed')
    assert generator.repairs == (2 if scenario == 'budget' else 1)
    diagnostic = await failure_diagnostic(owner, created.workflow_id)
    assert diagnostic['snapshot']['valid_checkpoint'] is False
    assert diagnostic['snapshot']['failure']['code'] == 'profile_geometry_invalid'
    async with tenant_transaction(owner.tenant_id, owner.principal_id) as conn:
        events = (await conn.execute(text('SELECT event_type,payload FROM task_events WHERE workflow_run_id=:id ORDER BY sequence'),
            {'id': created.workflow_id})).mappings().all()
        manifests = (await conn.execute(text('SELECT manifest,execution_attempt_id FROM agent_staging_manifests WHERE workflow_run_id=:id ORDER BY created_at'),
            {'id': created.workflow_id})).mappings().all()
        failures = (await conn.execute(text("SELECT id FROM execution_attempts WHERE workflow_run_id=:id AND status='failed'"),
            {'id': created.workflow_id})).scalars().all()
        assert not set(failures) & {m['execution_attempt_id'] for m in manifests}
        assert await conn.scalar(text('SELECT head_revision_id FROM project_branches WHERE id=:id'), {'id': initial.branch_id}) == initial.revision_id
        geometry = (await conn.execute(text("SELECT evidence FROM agent_validation_evidence WHERE workflow_run_id=:id AND gate='geometry'"),
            {'id': created.workflow_id})).scalars().all()
        if scenario == 'budget':
            assert not manifests and not geometry and len(failures) == 3
        else:
            assert len(manifests) == 2
            evidence = {c['check_id']: c for c in geometry[0]['acceptance']['evidence']}
            # At 24 mm, the frozen 25 mm probe is no longer on the requested
            # inner surface. Indeterminate must block just like a failed check.
            assert evidence['gap']['outcome'] == ('passed' if scenario == 'success' else 'indeterminate')
        if scenario != 'success':
            assert await conn.scalar(text('SELECT count(*) FROM change_sets WHERE source_workflow_run_id=:id'), {'id': created.workflow_id}) == 0
    verified = [e['payload'] for e in events if e['event_type'] == 'agent.freecad.profile_verified']
    assert len(verified) == (0 if scenario == 'budget' else 2)
    for item in verified:
        contract = await load_profile_contract(request, source_id=item['source_id'], source_hash=item['source_hash'])
        raw = json.loads(await get_object(evidence_key(request, item['sha256'])))
        native = raw['report']
        assert native['saved_reopened'] and len(native['parameter_probes']) >= 12
        assert contract['context']['acceptance']['checks'][0]['nominal'] == 25
        with pytest.raises(ValueError, match='owned'):
            await load_profile_contract(request.model_copy(update={'workflow_run_id': uuid4()}),
                source_id=item['source_id'], source_hash=item['source_hash'])
    if scenario == 'success':
        change_set_id = UUID(result['change_set_id'])
        from app.services.change_sets import ValidationRequired
        with pytest.raises(ValidationRequired):
            await accept_change_set(tenant_id=owner.tenant_id, reviewer_principal_id=owner.principal_id, change_set_id=change_set_id)
        await accept_change_set(tenant_id=owner.tenant_id, reviewer_principal_id=owner.principal_id, change_set_id=change_set_id,
            review_note='仅确认真实几何、参数联动和保存重开；外观及制造适配没有验收。')
        committed = await commit_change_set(tenant_id=owner.tenant_id, reviewer_principal_id=owner.principal_id, change_set_id=change_set_id)
        assert committed.status == 'committed'
    await Replayer(workflows=[McadAgentWorkflowV2]).replay_workflow(await handle.fetch_history())
