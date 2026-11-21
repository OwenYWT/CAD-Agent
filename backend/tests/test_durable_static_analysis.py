from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError

from app.agent.durable_repair import decide_repair
from app.agent.durable_plan import AffectedObject, AgentPlan, AgentPlanStep
from app.models.schemas import DesignBrief
from app.workflows.agent_v2 import McadAgentWorkflowV2
from app.workflows.handlers import cad_execution


class BackendMustNotRun:
    runtime_snapshot_calls = 0

    def runtime_snapshot(self):
        self.runtime_snapshot_calls += 1
        raise AssertionError("backend must not run for statically invalid source")


class BackendBoundaryReached:
    runtime_snapshot_calls = 0

    def runtime_snapshot(self):
        self.runtime_snapshot_calls += 1
        raise RuntimeError("backend boundary reached")


def _payload(source_code: str) -> dict:
    request_ids = {
        key: str(uuid4())
        for key in (
            "workflow_run_id",
            "tenant_id",
            "project_id",
            "principal_id",
            "branch_id",
            "expected_base_revision_id",
            "candidate_build_id",
            "source_id",
        )
    }
    step = AgentPlanStep(
        step_key="part-01",
        kind="assembly_part",
        description="Open rectangular enclosure body",
        affected_object_ids=("enclosure",),
        output_formats=("step",),
        part_name="body",
        part_dimensions={"length": 90.0, "width": 60.0, "height": 32.0},
        part_position=(0.0, 0.0, 0.0),
        part_color="lightgray",
    )
    plan = AgentPlan(
        objective="Create a body and lid enclosure",
        operation="generate",
        model_kind="assembly",
        modeling_strategy="assembly_parts",
        modeling_backend="cadquery",
        design_brief=DesignBrief(
            intent_summary="Open rectangular enclosure",
            artifact_type="enclosure",
        ),
        affected_objects=(
            AffectedObject(
                object_id="enclosure",
                object_type="assembly",
                label="Enclosure",
                change="create",
            ),
        ),
        steps=(step,),
    )
    return {
        **request_ids,
        "operation": "generate",
        "modeling_backend": "cadquery",
        "objective": plan.objective,
        "output_formats": ["step"],
        "plan": plan.temporal_payload(),
        "step": step.model_dump(mode="json"),
        "step_index": 3,
        "run_step_key": step.step_key,
        "run_step_kind": "agent_model",
        "source_code": source_code,
        "source_hash": hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
        "mode": "3d",
    }


@pytest.mark.asyncio
async def test_durable_execution_rejects_double_subtraction_before_backend(monkeypatch):
    source = (
        "import cadquery as cq\n"
        "result = cq.Workplane('XY').box(90, 60, 32)\n"
        "top_wp = result.faces('>Z').workplane()\n"
        "groove_cut = top_wp.rect(84, 54).cutBlind(-1.5)\n"
        "result = result - groove_cut\n"
        "show_object(result)\n"
    )
    backend = BackendMustNotRun()
    marked_failures: list[dict] = []

    @asynccontextmanager
    async def transaction(*_args, **_kwargs):
        yield object()

    async def no_replay(*_args, **_kwargs):
        return None

    async def prepare_attempt(*_args, **_kwargs):
        return uuid4(), uuid4(), "lease-token", 1

    async def mark_failure(_payload, **kwargs):
        marked_failures.append(kwargs)

    monkeypatch.setattr(cad_execution, "execution_identity", lambda: SimpleNamespace(attempt=1, activity_id="test"))
    monkeypatch.setattr(cad_execution, "tenant_transaction", transaction)
    monkeypatch.setattr(cad_execution, "get_staging_manifest_for_step", no_replay)
    monkeypatch.setattr(cad_execution, "_prepare_agent_execution_attempt", prepare_attempt)
    monkeypatch.setattr(cad_execution, "_mark_execution_failure", mark_failure)

    with pytest.raises(ApplicationError) as raised:
        await cad_execution.agent_execute_model(_payload(source), backend=backend)

    assert raised.value.type == "static_analysis_failed"
    assert raised.value.details[0]["category"] == "validation"
    assert raised.value.details[0]["runtime_error_type"] == "StaticAnalysis"
    assert "already applies the subtraction" in str(raised.value)
    assert backend.runtime_snapshot_calls == 0
    assert marked_failures[0]["error_code"] == "static_analysis_failed"

    failure = McadAgentWorkflowV2._execution_failure(raised.value)
    assert failure is not None
    decision = decide_repair(
        category=failure["category"],
        error_code=failure["error_code"],
        error_message=failure["error_message"],
        runtime_error_type=failure["runtime_error_type"],
        repair_count=0,
        seen_signatures=(),
    )
    assert decision.repairable is True
    assert decision.failure_class == "static_analysis"
    assert decision.strategy == "minimal_targeted_fix"


@pytest.mark.asyncio
async def test_durable_execution_does_not_block_benign_analyzer_warnings(monkeypatch):
    source = (
        "open_profile = cq.Workplane('XY').polyline([(0, 0), (1, 0)])\n"
        "independent = cq.Workplane('XY').circle(1).extrude(1)\n"
        "side_faces = body.faces('|Y')\n"
        "result = body.faces('>Z').workplane().hole(2)\n"
        "message = 'show_object(fake)'\n"
        "# show_object(fake)\n"
        "show_object(result)\n"
    )
    backend = BackendBoundaryReached()
    marked_failures: list[dict] = []

    @asynccontextmanager
    async def transaction(*_args, **_kwargs):
        yield object()

    async def no_replay(*_args, **_kwargs):
        return None

    async def prepare_attempt(*_args, **_kwargs):
        return uuid4(), uuid4(), "lease-token", 1

    async def mark_failure(_payload, **kwargs):
        marked_failures.append(kwargs)

    monkeypatch.setattr(cad_execution, "execution_identity", lambda: SimpleNamespace(attempt=1, activity_id="test"))
    monkeypatch.setattr(cad_execution, "tenant_transaction", transaction)
    monkeypatch.setattr(cad_execution, "get_staging_manifest_for_step", no_replay)
    monkeypatch.setattr(cad_execution, "_prepare_agent_execution_attempt", prepare_attempt)
    monkeypatch.setattr(cad_execution, "_mark_execution_failure", mark_failure)

    with pytest.raises(ApplicationError) as raised:
        await cad_execution.agent_execute_model(_payload(source), backend=backend)

    assert raised.value.type == "agent_model_execution_failed"
    assert "backend boundary reached" in str(raised.value)
    assert backend.runtime_snapshot_calls == 1
    assert marked_failures[0]["error_code"] == "agent_model_execution_failed"
