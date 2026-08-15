"""Real PostgreSQL coverage for durable Agent planning activities."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from temporalio.exceptions import ApplicationError

from app.agent.assembly_planner import AssemblyPlan
from app.agent.durable_planner import DurableAgentPlanner
from app.agent.multi_step import BuildPhase, BuildPlan, BuildStep
from app.config import settings
from app.db import close_database, get_database_engine, tenant_transaction
from app.domain.identity import user_principal
from app.models.schemas import CADPlan, DesignBrief
from app.repositories.identity import ensure_principal
from app.repositories.projects import create_project
from app.services.run_state import create_workflow
from app.workflows.activities import McadWorkflowActivities
from app.workflows.temporal import McadAgentWorkflowV2Request


ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CAD_AGENT_TEST_DATABASE_URL", "")
ORIGINAL_DATABASE_URL = settings.database_url
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="CAD_AGENT_TEST_DATABASE_URL is required for real PostgreSQL tests",
)


class PlannerStub:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.new_calls = 0

    async def plan_new(self, messages):
        self.new_calls += 1
        if self.error:
            raise self.error
        return CADPlan(
            description="创建带安装孔的支架",
            part_type="custom",
            dimensions={"length": 40, "width": 20, "height": 4},
            features=["底板", "安装孔", "加强筋"],
            constraints=[],
            modeling_hint="extrude_cut",
            design_brief=DesignBrief(
                intent_summary="创建安装支架",
                artifact_type="bracket",
            ),
        )


class DecomposerStub:
    def __init__(self):
        self.calls = 0

    async def decompose(self, plan, *, allow_fallback=True):
        self.calls += 1
        assert allow_fallback is False
        return BuildPlan(
            complexity="complex",
            steps=[
                BuildStep(BuildPhase.BASE, "创建 40x20x4 mm 底板"),
                BuildStep(BuildPhase.SECONDARY, "添加两个安装孔和加强筋"),
            ],
        )


class AssemblyPlannerStub:
    async def plan_assembly(self, plan, *, allow_fallback=True):
        return AssemblyPlan(parts=[], assembly_description="unused")


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    if not TEST_DATABASE_URL:
        yield
        return
    settings.database_url = TEST_DATABASE_URL
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture(scope="module", autouse=True, loop_scope="module")
async def database_engine_lifecycle(migrated_database):
    settings.database_url = TEST_DATABASE_URL
    yield
    await close_database()
    settings.database_url = ORIGINAL_DATABASE_URL


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def clean_control_plane():
    if not TEST_DATABASE_URL:
        yield
        return
    async with get_database_engine().begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE outbox_messages, task_events, execution_attempts, "
                "step_runs, workflow_runs, usage_meter_entries, audit_records, "
                "project_memberships, projects, tenant_memberships, principals, "
                "tenants CASCADE"
            )
        )
    yield


async def _request() -> McadAgentWorkflowV2Request:
    owner = user_principal(f"agent-owner-{uuid4()}")
    project_id = uuid4()
    async with tenant_transaction(
        owner.tenant_id,
        owner.principal_id,
    ) as connection:
        await ensure_principal(connection, owner)
        await create_project(
            connection,
            project_id=project_id,
            tenant_id=owner.tenant_id,
            creator_principal_id=owner.principal_id,
            name="Durable Agent",
            slug=f"durable-agent-{project_id.hex[:8]}",
        )
        created = await create_workflow(
            connection,
            tenant_id=owner.tenant_id,
            project_id=project_id,
            requested_by_principal_id=owner.principal_id,
            kind="mcad.agent.v2.generate",
            idempotency_key=f"agent-plan-{uuid4()}",
            request_payload={"objective": "创建带安装孔的支架"},
        )
    return McadAgentWorkflowV2Request(
        workflow_run_id=created.workflow_id,
        tenant_id=owner.tenant_id,
        project_id=project_id,
        principal_id=owner.principal_id,
        branch_id=uuid4(),
        expected_base_revision_id=uuid4(),
        operation="generate",
        objective="创建带安装孔的支架",
    )


@pytest.mark.asyncio(loop_scope="module")
async def test_planning_steps_persist_and_replay_without_new_provider_calls():
    request = await _request()
    planner = PlannerStub()
    decomposer = DecomposerStub()
    service = DurableAgentPlanner(
        planner=planner,
        decomposer=decomposer,
        assembly_planner=AssemblyPlannerStub(),
    )
    activities = McadWorkflowActivities(
        backend=object(),
        durable_planner=service,
    )
    payload = request.temporal_payload()

    requirements = await activities.agent_requirements(payload)
    decomposition = await activities.agent_decompose(
        {**payload, "requirements": requirements["requirements"]}
    )
    planned = await activities.agent_plan(
        {
            **payload,
            "requirements": requirements["requirements"],
            "decomposition": decomposition["decomposition"],
        }
    )
    replayed_requirements = await activities.agent_requirements(payload)
    replayed_decomposition = await activities.agent_decompose(
        {**payload, "requirements": requirements["requirements"]}
    )
    replayed_plan = await activities.agent_plan(
        {
            **payload,
            "requirements": requirements["requirements"],
            "decomposition": decomposition["decomposition"],
        }
    )

    async with tenant_transaction(
        request.tenant_id,
        request.principal_id,
    ) as connection:
        steps = (
            await connection.execute(
                text(
                    "SELECT step_key, step_index, status FROM step_runs "
                    "WHERE workflow_run_id=:workflow_id ORDER BY step_index"
                ),
                {"workflow_id": request.workflow_run_id},
            )
        ).mappings().all()
        agent_events = (
            await connection.execute(
                text(
                    "SELECT event_type FROM task_events "
                    "WHERE workflow_run_id=:workflow_id "
                    "AND event_type LIKE 'agent.%' ORDER BY sequence"
                ),
                {"workflow_id": request.workflow_run_id},
            )
        ).scalars().all()
        workflow_status = await connection.scalar(
            text("SELECT status FROM workflow_runs WHERE id=:id"),
            {"id": request.workflow_run_id},
        )

    assert planner.new_calls == 1
    assert decomposer.calls == 1
    assert [row["step_key"] for row in steps] == [
        "agent-requirements",
        "agent-decompose",
        "agent-plan",
    ]
    assert [row["status"] for row in steps] == ["succeeded"] * 3
    assert agent_events == [
        "agent.requirements.completed",
        "agent.decomposition.completed",
        "agent.plan.completed",
    ]
    assert workflow_status == "running"
    assert planned["plan"]["model_kind"] == "complex"
    assert replayed_requirements["replayed"] is True
    assert replayed_decomposition["replayed"] is True
    assert replayed_plan["replayed"] is True


@pytest.mark.asyncio(loop_scope="module")
async def test_planning_failure_marks_step_failed_without_static_fallback():
    request = await _request()
    activities = McadWorkflowActivities(
        backend=object(),
        durable_planner=DurableAgentPlanner(
            planner=PlannerStub(error=RuntimeError("provider unavailable")),
            decomposer=DecomposerStub(),
            assembly_planner=AssemblyPlannerStub(),
        ),
    )

    with pytest.raises(ApplicationError, match="provider unavailable"):
        await activities.agent_requirements(request.temporal_payload())

    async with tenant_transaction(
        request.tenant_id,
        request.principal_id,
    ) as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT status, error_code FROM step_runs "
                    "WHERE workflow_run_id=:workflow_id "
                    "AND step_key='agent-requirements'"
                ),
                {"workflow_id": request.workflow_run_id},
            )
        ).mappings().one()

    assert stored["status"] == "failed"
    assert stored["error_code"] == "RuntimeError"
