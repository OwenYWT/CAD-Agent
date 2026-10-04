"""Compatibility and Temporal registration adapter; business logic lives in handlers."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from uuid import UUID
from temporalio import activity
from app.agent.durable_planner import DurableAgentPlanner
from app.agent.durable_repair import DurableRepairSourceGenerator
from app.agent.orchestrator import Orchestrator
from app.execution.backend import ExecutionBackend
from app.execution.composition import get_execution_backend
from app.execution.contracts import ArtifactInput
from app.freecad.operation_generator import FreeCADOperationGenerator
from app.freecad.constraint_patch import NativeConstraintRepairValidation
from app.validation.durable_visual import DurableVisualValidator
from app.workflows.source_preparation import SourcePreparer
from app.workflows.modeling import DurableModelingSourceGenerator
from app.models.workflow_requests import (McadAgentWorkflowV2Request)
from app.workflows.handlers.decomposition import decompose
from app.workflows.errors import planning_error

from app.workflows import execution_support
from app.workflows.execution_support import _worker_id, _modeling_outputs, _stored_execution_result, _prepare_execution_attempt, _prepare_agent_execution_attempt, _prepare_agent_validation_attempt, _stored_validation_attempt_result, _await_provider_operation, _heartbeat_loop, _run_backend_with_heartbeats, interrupted_execution_status, _mark_execution_failure
from app.workflows import validation_support
from app.workflows.validation_support import _record_agent_validation_outcome, _complete_check_workflow, _record_freecad_inspections
from app.workflows import revision_inputs
from app.workflows.revision_inputs import _revision_restore_operation_plan, _freecad_revision_artifact, _freecad_revision_state, _agent_validation_input
from app.workflows.handlers import planning
from app.workflows.handlers import source_generation
from app.workflows.handlers import native_generation
from app.workflows.handlers import cad_execution
from app.workflows.handlers import geometry_validation
from app.workflows.handlers import visual_validation
from app.workflows.handlers import dfm_validation
from app.workflows.handlers import bom
from app.workflows.handlers import candidates
from app.workflows.handlers import legacy_modeling
from app.workflows.handlers import engineering
from app.workflows.handlers import review
from app.workflows.handlers import lifecycle
from app.workflows.errors import planning_error
from app.workflows.logical_steps import _stored_agent_step_result, _start_agent_logical_step, _complete_agent_logical_step
from app.db import tenant_transaction
from app.repositories.agent_candidates import get_staging_manifest_for_step
from app.repositories.artifacts import committed_artifact_for_revision
from app.object_store import download_object

class McadWorkflowActivities:
    def __init__(
        self,
        backend: ExecutionBackend | None = None,
        *,
        source_preparer: SourcePreparer | None = None,
        durable_planner: DurableAgentPlanner | None = None,
        durable_modeling: DurableModelingSourceGenerator | None = None,
        durable_repair: DurableRepairSourceGenerator | None = None,
        durable_visual: DurableVisualValidator | None = None,
        freecad_operations: FreeCADOperationGenerator | None = None,
    ):
        self.backend = backend or get_execution_backend()
        self.source_preparer = source_preparer or SourcePreparer(
            Orchestrator(execution_backend=self.backend)
        )
        self.durable_planner = durable_planner or DurableAgentPlanner()
        self.durable_modeling = durable_modeling or DurableModelingSourceGenerator()
        self.durable_repair = durable_repair or DurableRepairSourceGenerator()
        self.durable_visual = durable_visual or DurableVisualValidator()
        self.freecad_operations = freecad_operations or FreeCADOperationGenerator()


    async def _freecad_revision_artifact(self, request: McadAgentWorkflowV2Request, artifact_kind: str) -> dict[str, Any]:
        return await revision_inputs._freecad_revision_artifact(request, artifact_kind)


    async def _freecad_revision_state(self, request: McadAgentWorkflowV2Request) -> dict[str, Any]:
        return await revision_inputs._freecad_revision_state(request)


    _agent_planning_error = staticmethod(planning_error)


    @activity.defn(name='agent_v2.requirements')
    async def agent_requirements(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await planning.agent_requirements(payload, durable_planner=self.durable_planner)


    @activity.defn(name="agent_v2.decompose")
    async def agent_decompose(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await decompose(payload, planner=self.durable_planner)


    @activity.defn(name='agent_v2.plan')
    async def agent_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await planning.agent_plan(payload, durable_planner=self.durable_planner)


    @activity.defn(name='agent_v2.allocate_candidate')
    async def agent_allocate_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await candidates.agent_allocate_candidate(payload)


    @activity.defn(name='agent_v2.terminate_candidate')
    async def agent_terminate_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await candidates.agent_terminate_candidate(payload)


    @activity.defn(name='agent_v2.generate_source')
    async def agent_generate_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await source_generation.agent_generate_source(payload, durable_modeling=self.durable_modeling)


    @activity.defn(name='agent_v2.repair_source')
    async def agent_repair_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await source_generation.agent_repair_source(payload, durable_repair=self.durable_repair)


    @activity.defn(name='agent_v2.generate_operations')
    async def agent_generate_operations(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await native_generation.agent_generate_operations(payload, freecad_operations=self.freecad_operations)


    @activity.defn(name='agent_v2.repair_operations')
    async def agent_repair_operations(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await native_generation.agent_repair_operations(payload, freecad_operations=self.freecad_operations)


    @activity.defn(name='agent_v2.execute_model')
    async def agent_execute_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await cad_execution.agent_execute_model(payload, backend=self.backend)


    @activity.defn(name='agent_v2.execute_freecad')
    async def agent_execute_freecad(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await cad_execution.agent_execute_freecad(payload, backend=self.backend,
            constraint_validation=NativeConstraintRepairValidation())


    @activity.defn(name='agent_v2.validate_geometry')
    async def agent_validate_geometry(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await geometry_validation.agent_validate_geometry(payload, backend=self.backend)


    async def _agent_validation_input(self, *, request: McadAgentWorkflowV2Request, candidate_build_id: UUID, manifest_id: UUID, temp_dir: Path, preferred_format: str='stl') -> tuple[dict[str, Any], ArtifactInput, dict[str, Path]]:
        return await revision_inputs._agent_validation_input(request=request, candidate_build_id=candidate_build_id, manifest_id=manifest_id, temp_dir=temp_dir, preferred_format=preferred_format)


    @activity.defn(name='agent_v2.render_visual')
    async def agent_render_visual(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await visual_validation.agent_render_visual(payload, backend=self.backend)


    @activity.defn(name='agent_v2.judge_visual')
    async def agent_judge_visual(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await visual_validation.agent_judge_visual(payload, durable_visual=self.durable_visual)


    @activity.defn(name='agent_v2.repair_visual')
    async def agent_repair_visual(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await visual_validation.agent_repair_visual(payload, durable_visual=self.durable_visual)


    @activity.defn(name='agent_v2.validate_dfm')
    async def agent_validate_dfm(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await dfm_validation.agent_validate_dfm(payload, backend=self.backend)


    @activity.defn(name='agent_v2.generate_bom')
    async def agent_generate_bom(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await bom.agent_generate_bom(payload, backend=self.backend)


    @activity.defn(name='agent_v2.seal_candidate')
    async def agent_seal_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await candidates.agent_seal_candidate(payload)


    @activity.defn(name='mcad.prepare_source')
    async def prepare_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await legacy_modeling.prepare_source(payload, source_preparer=self.source_preparer)


    @activity.defn(name='mcad.plan')
    async def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await legacy_modeling.plan(payload)


    @activity.defn(name='mcad.execute')
    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await legacy_modeling.execute(payload, backend=self.backend)


    @activity.defn(name='engineering.compute')
    async def engineering_compute(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await engineering.engineering_compute(payload, backend=self.backend)


    @activity.defn(name='scene.compute')
    async def scene_compute(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await engineering.scene_compute(payload, backend=self.backend)


    @activity.defn(name='mcad.check')
    async def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await engineering.check(payload, backend=self.backend)


    @activity.defn(name='mcad.validate')
    async def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await review.validate(payload)


    @activity.defn(name='mcad.wait_confirmation')
    async def wait_confirmation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await review.wait_confirmation(payload)


    @activity.defn(name='mcad.resume_after_confirmation')
    async def resume_after_confirmation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await review.resume_after_confirmation(payload)


    @activity.defn(name='mcad.finalize')
    async def finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await review.finalize(payload)


    @activity.defn(name='mcad.record_cancel')
    async def record_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await lifecycle.record_cancel(payload)


    @activity.defn(name='mcad.record_timeout')
    async def record_timeout(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await lifecycle.record_timeout(payload)


    @activity.defn(name='mcad.record_failure')
    async def record_failure(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await lifecycle.record_failure(payload)


    @activity.defn(name='document.acquire')
    async def acquire_document(self, payload: dict) -> dict:
        return await lifecycle.acquire_document(payload)


    @activity.defn(name='document.release')
    async def release_document(self, payload: dict) -> dict:
        return await lifecycle.release_document(payload)


    def registered(self) -> list:
        return [
            self.acquire_document,
            self.release_document,
            self.prepare_source,
            self.plan,
            self.execute,
            self.check,
            self.engineering_compute,
            self.scene_compute,
            self.validate,
            self.wait_confirmation,
            self.resume_after_confirmation,
            self.finalize,
            self.record_cancel,
            self.record_timeout,
            self.record_failure,
        ]


    def registered_agent_v2(self) -> list:
        """Activities available only to the version-isolated Agent queue."""
        return [
            self.acquire_document,
            self.release_document,
            self.agent_requirements,
            self.agent_decompose,
            self.agent_plan,
            self.agent_allocate_candidate,
            self.agent_terminate_candidate,
            self.agent_generate_source,
            self.agent_generate_operations,
            self.agent_repair_source,
            self.agent_repair_operations,
            self.agent_execute_model,
            self.agent_execute_freecad,
            self.agent_validate_geometry,
            self.agent_render_visual,
            self.agent_judge_visual,
            self.agent_repair_visual,
            self.agent_validate_dfm,
            self.agent_generate_bom,
            self.agent_seal_candidate,
            self.wait_confirmation,
            self.resume_after_confirmation,
            self.record_cancel,
            self.record_timeout,
            self.record_failure,
        ]
