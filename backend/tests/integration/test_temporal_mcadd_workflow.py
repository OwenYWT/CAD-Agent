"""Real Temporal + PostgreSQL + MinIO + Podman MCAD workflow tests."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import socket
import sys
from pathlib import Path
from uuid import uuid4

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
        os.environ.get("CAD_AGENT_TEST_REAL_LLM") == "1"
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
                                 jsonb_array_elements(manifest->'outputs') output
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

    await clean()
    yield
    await clean()


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
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"workflow {workflow_id} did not reach {sorted(expected)}"
    )


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
                    description="创建 30x20x4 mm 底座",
                    dimensions={"length": 30, "width": 20, "height": 4},
                    position=[0, 0, 0],
                    color="lightgray",
                ),
                AssemblyPart(
                    name="lid",
                    description="创建 30x20x2 mm 上盖",
                    dimensions={"length": 30, "width": 20, "height": 2},
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
            height = 4 if step.step_key == "part-01" else 2
            source = (
                "import cadquery as cq\n"
                f"def make_{step.step_key.replace('-', '_')}():\n"
                f"    return cq.Workplane('XY').box(30, 20, {height})\n"
                f"result = make_{step.step_key.replace('-', '_')}()\n"
            )
        else:
            assert len(requirements["part_sources"]) == 2
            source = (
                "import cadquery as cq\n"
                "def make_part_01(): return cq.Workplane('XY').box(30,20,4)\n"
                "def make_part_02(): return cq.Workplane('XY').box(30,20,2)\n"
                "result = cq.Assembly()\n"
                "result.add(make_part_01(), name='base')\n"
                "result.add(make_part_02(), name='lid', "
                "loc=cq.Location((0,0,5)))\n"
            )
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
                    f"http://127.0.0.1:{port}/ready",
                    timeout=1,
                )
                if response.status_code == 200:
                    assert response.json()["status"] == "ready"
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
        from app.services.run_state import create_workflow

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
        from app.services.run_state import create_workflow

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
        "warn",
    ]
    assert projected["agent.candidate.sealed"]["stage"] == "review"
    assert projected["agent.candidate.sealed"]["risk_count"] == 2
    change_detail = await get_change_set_detail(
        owner,
        candidate["change_set_id"],
    )
    assert change_detail["risk_summary"]["issue_count"] == 2
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
        planner=_V2PlannerStub(),
        decomposer=_V2DecomposerStub(),
        assembly_planner=_V2AssemblyStub(),
    )
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        from app.services.run_state import create_workflow

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
        await _wait_for_status(owner, created.workflow_id, {"waiting_confirmation"})
        await confirm_mcad_workflow(
            created.workflow_id,
            accepted=True,
            note="确认真实视觉服务测试",
            workflow_kind="mcad.agent.v2.generate",
        )
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
        from app.services.run_state import create_workflow

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
        ("dfm", "failed"),
    ], json.dumps(diagnostic, ensure_ascii=False, default=str, sort_keys=True)
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
        from app.services.run_state import create_workflow

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
        from app.services.run_state import create_workflow

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
        from app.services.run_state import create_workflow

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
        from app.services.run_state import create_workflow

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
        from app.services.run_state import create_workflow

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
        from app.services.run_state import create_workflow

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
        from app.services.run_state import create_workflow

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
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), timeout=360)
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
        from app.services.run_state import create_workflow

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
        await _wait_for_status(owner, workflow_id, {"running"})
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
        workflow_id, handle = await start_mcad_workflow(
            tenant_id=owner.tenant_id,
            project_id=project_id,
            principal_id=owner.principal_id,
            branch_id=initial.branch_id,
            expected_base_revision_id=initial.revision_id,
            kind="modify",
            idempotency_key=f"stale-workflow-{project_id}",
            objective="基于过期版本修改",
            primary=_box_execution(),
        )
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
        "SANDBOX_RUNTIME": "podman",
        "SANDBOX_COMMAND": "podman",
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
        restarted_api = await _start_api(env, api_port)
        await _wait_for_status(
            owner,
            workflow_id,
            {"waiting_confirmation"},
            timeout=45,
        )
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
