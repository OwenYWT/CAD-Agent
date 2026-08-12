"""Versioned, execution-free plan contract for the durable Agent workflow."""
from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.schemas import DesignBrief


Identifier = str
OutputFormat = Literal["step", "stl", "dxf", "svg", "png", "json"]


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfirmationPolicy(str, Enum):
    NONE = "none"
    REQUIRED = "required"


class GateMode(str, Enum):
    REQUIRED = "required"
    ADVISORY = "advisory"
    DISABLED = "disabled"


class ValidationGatePolicy(FrozenContract):
    mode: GateMode
    repair_budget: int = Field(default=0, ge=0, le=5)

    @model_validator(mode="after")
    def disabled_gate_has_no_repairs(self) -> "ValidationGatePolicy":
        if self.mode is GateMode.DISABLED and self.repair_budget:
            raise ValueError("disabled validation gate cannot have a repair budget")
        return self


class AgentValidationPolicy(FrozenContract):
    artifact_integrity: ValidationGatePolicy = Field(
        default_factory=lambda: ValidationGatePolicy(
            mode=GateMode.REQUIRED,
            repair_budget=0,
        )
    )
    geometry: ValidationGatePolicy = Field(
        default_factory=lambda: ValidationGatePolicy(
            mode=GateMode.REQUIRED,
            repair_budget=2,
        )
    )
    visual: ValidationGatePolicy = Field(
        default_factory=lambda: ValidationGatePolicy(
            mode=GateMode.ADVISORY,
            repair_budget=1,
        )
    )
    dfm: ValidationGatePolicy = Field(
        default_factory=lambda: ValidationGatePolicy(
            mode=GateMode.ADVISORY,
            repair_budget=0,
        )
    )

    @model_validator(mode="after")
    def integrity_is_always_required(self) -> "AgentValidationPolicy":
        if self.artifact_integrity.mode is not GateMode.REQUIRED:
            raise ValueError("artifact integrity must be a required gate")
        return self


class AffectedObject(FrozenContract):
    object_id: Identifier = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    object_type: Literal["part", "assembly", "feature", "profile", "file"]
    label: str = Field(min_length=1, max_length=240)
    change: Literal["create", "modify", "remove", "inspect"]


class AgentPlanStep(FrozenContract):
    step_key: Identifier = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9_-]*$",
    )
    kind: Literal[
        "model",
        "assembly_part",
        "assembly_combine",
        "modify",
        "export",
    ]
    description: str = Field(min_length=1, max_length=4000)
    depends_on: tuple[Identifier, ...] = ()
    affected_object_ids: tuple[Identifier, ...] = ()
    output_formats: tuple[OutputFormat, ...] = ()

    @model_validator(mode="after")
    def references_are_unique(self) -> "AgentPlanStep":
        for label, values in (
            ("depends_on", self.depends_on),
            ("affected_object_ids", self.affected_object_ids),
            ("output_formats", self.output_formats),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} cannot contain duplicates")
        if self.step_key in self.depends_on:
            raise ValueError("step cannot depend on itself")
        return self


class AgentPlan(FrozenContract):
    schema_version: Literal["durable-agent-plan.v1"] = (
        "durable-agent-plan.v1"
    )
    objective: str = Field(min_length=1, max_length=4000)
    operation: Literal["generate", "modify"]
    model_kind: Literal["simple", "complex", "assembly", "profile_2d"]
    modeling_strategy: str = Field(min_length=1, max_length=120)
    design_brief: DesignBrief
    expected_base_revision_id: UUID | None = None
    affected_objects: tuple[AffectedObject, ...]
    steps: tuple[AgentPlanStep, ...]
    confirmation_policy: ConfirmationPolicy = ConfirmationPolicy.NONE
    confirmation_reason: str | None = Field(default=None, max_length=1000)
    validation_policy: AgentValidationPolicy = Field(
        default_factory=AgentValidationPolicy
    )

    @model_validator(mode="after")
    def validate_graph_and_policy(self) -> "AgentPlan":
        if not self.affected_objects:
            raise ValueError("Agent plan requires at least one affected object")
        if not self.steps:
            raise ValueError("Agent plan requires at least one modeling step")

        object_ids = [item.object_id for item in self.affected_objects]
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("affected object IDs must be unique")

        seen_steps: set[str] = set()
        known_objects = set(object_ids)
        for step in self.steps:
            if step.step_key in seen_steps:
                raise ValueError("step keys must be unique")
            missing_dependencies = set(step.depends_on) - seen_steps
            if missing_dependencies:
                raise ValueError(
                    "step dependency must reference an earlier step: "
                    + ", ".join(sorted(missing_dependencies))
                )
            unknown_objects = set(step.affected_object_ids) - known_objects
            if unknown_objects:
                raise ValueError(
                    "step references unknown affected object: "
                    + ", ".join(sorted(unknown_objects))
                )
            seen_steps.add(step.step_key)

        if (
            self.operation == "modify"
            and self.confirmation_policy is not ConfirmationPolicy.REQUIRED
        ):
            raise ValueError("modification plan requires confirmation")
        if self.operation == "modify" and self.expected_base_revision_id is None:
            raise ValueError("modification plan requires expected base revision")
        if (
            self.confirmation_policy is ConfirmationPolicy.REQUIRED
            and not (self.confirmation_reason or "").strip()
        ):
            object.__setattr__(
                self,
                "confirmation_reason",
                "计划会修改或创建工程对象，请确认后执行。",
            )
        return self

    def temporal_payload(self) -> dict:
        """Return the complete deterministic payload stored in Temporal history."""
        return self.model_dump(mode="json", exclude_none=False)
