"""Contracts for the versioned durable Agent plan and V2 queue boundary."""
from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.durable_plan import (
    AffectedObject,
    AgentPlan,
    AgentPlanStep,
    AgentValidationPolicy,
    ConfirmationPolicy,
    GateMode,
    ValidationGatePolicy,
    normalize_agent_plan_backend,
)
from app.config import Settings


def _plan(**overrides) -> AgentPlan:
    values = {
        "objective": "创建 20 x 10 x 4 mm 支架",
        "operation": "generate",
        "model_kind": "simple",
        "modeling_strategy": "extrude_cut",
        "design_brief": {
            "intent_summary": "创建一个简单安装支架",
            "artifact_type": "bracket",
            "acceptance_criteria": ["输出 STEP 和 STL"],
        },
        "affected_objects": [
            AffectedObject(
                object_id="part-main",
                object_type="part",
                label="主支架",
                change="create",
            )
        ],
        "steps": [
            AgentPlanStep(
                step_key="model-main",
                kind="model",
                description="生成参数化主支架",
                affected_object_ids=("part-main",),
                output_formats=("step", "stl"),
            )
        ],
    }
    values.update(overrides)
    return AgentPlan(**values)


def test_simple_plan_has_deterministic_strict_temporal_payload():
    plan = _plan()

    assert plan.schema_version == "durable-agent-plan.v1"
    assert plan.temporal_payload() == plan.model_dump(
        mode="json",
        exclude_none=False,
    )
    assert plan.temporal_payload()["steps"][0]["step_key"] == "model-main"
    with pytest.raises(ValidationError):
        AgentPlan(**{**plan.model_dump(), "source_code": "print('unsafe')"})
    with pytest.raises(ValidationError):
        plan.objective = "mutated"


def test_complex_plan_requires_ordered_existing_dependencies():
    plan = _plan(
        model_kind="complex",
        steps=[
            AgentPlanStep(
                step_key="base",
                kind="model",
                description="创建基础实体",
                affected_object_ids=("part-main",),
            ),
            AgentPlanStep(
                step_key="holes",
                kind="model",
                description="添加安装孔",
                depends_on=("base",),
                affected_object_ids=("part-main",),
                output_formats=("step", "stl"),
            ),
        ],
    )

    assert plan.steps[1].depends_on == ("base",)
    with pytest.raises(ValidationError, match="earlier step"):
        _plan(
            model_kind="complex",
            steps=[
                AgentPlanStep(
                    step_key="holes",
                    kind="model",
                    description="添加安装孔",
                    depends_on=("base",),
                    affected_object_ids=("part-main",),
                ),
                AgentPlanStep(
                    step_key="base",
                    kind="model",
                    description="创建基础实体",
                    affected_object_ids=("part-main",),
                ),
            ],
        )


def test_assembly_plan_rejects_unknown_or_duplicate_objects():
    objects = [
        AffectedObject(
            object_id="housing",
            object_type="part",
            label="壳体",
            change="create",
        ),
        AffectedObject(
            object_id="shaft",
            object_type="part",
            label="轴",
            change="create",
        ),
    ]
    plan = _plan(
        model_kind="assembly",
        affected_objects=objects,
        steps=[
            AgentPlanStep(
                step_key="housing",
                kind="assembly_part",
                description="生成壳体",
                affected_object_ids=("housing",),
                part_name="housing",
                part_position=(0, 0, 0),
                part_color="lightgray",
            ),
            AgentPlanStep(
                step_key="shaft",
                kind="assembly_part",
                description="生成轴",
                affected_object_ids=("shaft",),
                part_name="shaft",
                part_position=(0, 0, 0),
                part_color="steelblue",
            ),
            AgentPlanStep(
                step_key="combine",
                kind="assembly_combine",
                description="组合装配体",
                depends_on=("housing", "shaft"),
                affected_object_ids=("housing", "shaft"),
                output_formats=("step", "stl"),
            ),
        ],
    )

    assert plan.model_kind == "assembly"
    with pytest.raises(ValidationError, match="unknown affected object"):
        _plan(
            steps=[
                AgentPlanStep(
                    step_key="model-main",
                    kind="model",
                    description="生成模型",
                    affected_object_ids=("missing",),
                )
            ]
        )


def test_modification_requires_pre_execution_confirmation():
    with pytest.raises(ValidationError, match="confirmation"):
        _plan(
            operation="modify",
            confirmation_policy=ConfirmationPolicy.NONE,
        )

    plan = _plan(
        operation="modify",
        confirmation_policy=ConfirmationPolicy.REQUIRED,
        expected_base_revision_id=uuid4(),
    )
    assert plan.confirmation_policy is ConfirmationPolicy.REQUIRED


def test_backend_policy_normalizes_generate_and_modify_before_persistence():
    simple = normalize_agent_plan_backend(
        operation="generate",
        request_modeling_backend="auto",
        plan_candidate=_plan(),
    )
    assembly = normalize_agent_plan_backend(
        operation="generate",
        request_modeling_backend="auto",
        plan_candidate=_plan(model_kind="assembly"),
    )
    modification = normalize_agent_plan_backend(
        operation="modify",
        request_modeling_backend="freecad",
        plan_candidate=_plan(
            operation="modify",
            confirmation_policy=ConfirmationPolicy.REQUIRED,
            expected_base_revision_id=uuid4(),
        ),
    )

    assert simple.modeling_backend == "freecad"
    assert assembly.modeling_backend == "cadquery"
    assert modification.modeling_backend == "freecad"


def test_modify_backend_policy_rejects_planner_mismatch():
    with pytest.raises(ValueError, match="agent_plan_backend_mismatch"):
        normalize_agent_plan_backend(
            operation="modify",
            request_modeling_backend="freecad",
            plan_candidate=_plan(
                operation="modify",
                modeling_backend="cadquery",
                confirmation_policy=ConfirmationPolicy.REQUIRED,
                expected_base_revision_id=uuid4(),
            ),
        )


def test_validation_policy_distinguishes_required_advisory_and_disabled():
    policy = AgentValidationPolicy(
        geometry=ValidationGatePolicy(mode=GateMode.REQUIRED, repair_budget=2),
        visual=ValidationGatePolicy(mode=GateMode.ADVISORY, repair_budget=1),
        dfm=ValidationGatePolicy(mode=GateMode.DISABLED, repair_budget=0),
    )
    plan = _plan(validation_policy=policy)

    assert plan.validation_policy.geometry.mode is GateMode.REQUIRED
    assert plan.validation_policy.visual.mode is GateMode.ADVISORY
    assert plan.validation_policy.dfm.mode is GateMode.DISABLED


def test_v2_queue_is_dedicated():
    settings = Settings(_env_file=None)

    assert settings.temporal_agent_v2_task_queue
    assert settings.temporal_agent_v2_task_queue != settings.temporal_task_queue

    with pytest.raises(ValueError, match="different"):
        Settings(
            _env_file=None,
            temporal_task_queue="shared",
            temporal_agent_v2_task_queue="shared",
        )


def test_enabled_v2_routing_requires_a_non_empty_v2_queue():
    settings = Settings(
        _env_file=None,
        durable_control_plane_enabled=True,
        temporal_agent_v2_task_queue=" ",
    )

    assert any(
        "TEMPORAL_AGENT_V2_TASK_QUEUE" in problem
        for problem in settings.durable_control_plane_config_problems()
    )
