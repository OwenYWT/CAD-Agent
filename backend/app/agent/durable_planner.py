"""Planning-only adapter from the proven Agent planners to AgentPlan v1."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.agent.assembly_planner import AssemblyPlan, AssemblyPlanner
from app.agent.design_brief import ensure_design_brief
from app.agent.durable_plan import (
    AffectedObject,
    AgentPlan,
    AgentPlanStep,
    ConfirmationPolicy,
)
from app.agent.multi_step import BuildPhase, BuildPlan, BuildStep, PlanDecomposer
from app.agent.orchestrator import requires_design_confirmation
from app.agent.planner import Planner
from app.models.schemas import CADPlan, DesignBrief, ModificationPlan


class DurableAgentPlanner:
    """Create an execution-free durable plan from existing planning assets."""

    def __init__(
        self,
        *,
        planner: Planner | None = None,
        decomposer: PlanDecomposer | None = None,
        assembly_planner: AssemblyPlanner | None = None,
    ) -> None:
        self.planner = planner or Planner()
        self.decomposer = decomposer or PlanDecomposer()
        self.assembly_planner = assembly_planner or AssemblyPlanner()

    @staticmethod
    def _is_simple(plan: CADPlan) -> bool:
        return (
            plan.part_type not in {"custom", "organic", "assembly"}
            and len(plan.features) <= 2
            and len(plan.constraints) <= 2
        )

    @staticmethod
    def _confirmation(brief: DesignBrief) -> tuple[ConfirmationPolicy, str | None]:
        if not requires_design_confirmation(brief):
            return ConfirmationPolicy.NONE, None
        question = next(
            (
                item
                for item in brief.open_questions
                if any(
                    marker in item
                    for marker in (
                        "必须确认",
                        "无法安全默认",
                        "无法继续",
                        "无法生成",
                        "必须先确认",
                    )
                )
            ),
            brief.open_questions[0] if brief.open_questions else "必须先确认需求",
        )
        return ConfirmationPolicy.REQUIRED, question

    @staticmethod
    def _strategy(plan: CADPlan) -> str:
        if plan.modeling_hint.strip():
            return plan.modeling_hint.strip()
        if plan.part_type == "profile_2d":
            return "profile_2d"
        if plan.part_type == "assembly":
            return "assembly_combine"
        return "parametric"

    async def plan_generation(
        self,
        objective: str,
        *,
        output_formats: tuple[str, ...] = ("step", "stl"),
    ) -> AgentPlan:
        plan = await self.requirements_generation(objective)
        decomposition = await self.decompose_generation(plan)
        return self.compose_generation(
            objective,
            plan,
            decomposition=decomposition,
            output_formats=output_formats,
        )

    async def requirements_generation(self, objective: str) -> CADPlan:
        """Run only requirements interpretation; never generate CAD source."""
        return await self.planner.plan_new(
            [{"role": "user", "content": objective}]
        )

    async def decompose_generation(
        self,
        plan: CADPlan,
    ) -> dict[str, Any] | None:
        """Return a JSON-safe decomposition for plans that need another call."""
        if plan.part_type == "assembly":
            assembly = await self.assembly_planner.plan_assembly(
                plan,
                allow_fallback=False,
            )
            return {
                "kind": "assembly",
                "assembly": assembly.model_dump(mode="json"),
            }
        if self._is_simple(plan):
            return None
        build = await self.decomposer.decompose(
            plan,
            allow_fallback=False,
        )
        return {
            "kind": "build",
            "complexity": build.complexity,
            "steps": [
                {
                    "phase": step.phase.value,
                    "description": step.description,
                }
                for step in build.steps
            ],
        }

    def compose_generation(
        self,
        objective: str,
        plan: CADPlan,
        *,
        decomposition: dict[str, Any] | None,
        output_formats: tuple[str, ...] = ("step", "stl"),
    ) -> AgentPlan:
        """Map persisted requirements/decomposition into the strict plan."""
        brief = ensure_design_brief(plan)
        confirmation, reason = self._confirmation(brief)

        if plan.part_type == "assembly":
            if not decomposition or decomposition.get("kind") != "assembly":
                raise ValueError("assembly plan requires assembly decomposition")
            return self._assembly_plan(
                objective,
                plan,
                brief,
                confirmation,
                reason,
                output_formats,
                AssemblyPlan.model_validate(decomposition["assembly"]),
            )

        affected = (
            AffectedObject(
                object_id="part-main",
                object_type=(
                    "profile" if plan.part_type == "profile_2d" else "part"
                ),
                label=brief.intent_summary or plan.description,
                change="create",
            ),
        )
        formats = (
            ("dxf",)
            if plan.part_type == "profile_2d"
            else tuple(output_formats)
        )
        if self._is_simple(plan):
            if decomposition is not None:
                raise ValueError("simple plan cannot include decomposition")
            steps = (
                AgentPlanStep(
                    step_key="model-main",
                    kind="model",
                    description=plan.description,
                    affected_object_ids=("part-main",),
                    output_formats=formats,
                ),
            )
            model_kind = (
                "profile_2d"
                if plan.part_type == "profile_2d"
                else "simple"
            )
        else:
            if not decomposition or decomposition.get("kind") != "build":
                raise ValueError("complex plan requires build decomposition")
            build = BuildPlan(
                complexity=str(decomposition.get("complexity") or "complex"),
                steps=[
                    BuildStep(
                        phase=BuildPhase(str(step["phase"])),
                        description=str(step["description"]),
                    )
                    for step in decomposition.get("steps") or []
                ],
            )
            if not build.steps:
                raise ValueError("build decomposition cannot be empty")
            mapped: list[AgentPlanStep] = []
            previous: str | None = None
            for index, step in enumerate(build.steps, start=1):
                key = f"model-{index:02d}-{step.phase.value}"
                mapped.append(
                    AgentPlanStep(
                        step_key=key,
                        kind="model",
                        description=step.description,
                        depends_on=(previous,) if previous else (),
                        affected_object_ids=("part-main",),
                        output_formats=(
                            formats if index == len(build.steps) else ()
                        ),
                    )
                )
                previous = key
            steps = tuple(mapped)
            model_kind = "complex"

        return AgentPlan(
            objective=objective,
            operation="generate",
            model_kind=model_kind,
            modeling_strategy=self._strategy(plan),
            design_brief=brief,
            affected_objects=affected,
            steps=steps,
            confirmation_policy=confirmation,
            confirmation_reason=reason,
        )

    def _assembly_plan(
        self,
        objective: str,
        plan: CADPlan,
        brief: DesignBrief,
        confirmation: ConfirmationPolicy,
        reason: str | None,
        output_formats: tuple[str, ...],
        assembly: AssemblyPlan,
    ) -> AgentPlan:
        objects: list[AffectedObject] = []
        steps: list[AgentPlanStep] = []
        part_keys: list[str] = []
        for index, part in enumerate(assembly.parts, start=1):
            key = f"part-{index:02d}"
            part_keys.append(key)
            objects.append(
                AffectedObject(
                    object_id=key,
                    object_type="part",
                    label=part.name,
                    change="create",
                )
            )
            steps.append(
                AgentPlanStep(
                    step_key=key,
                    kind="assembly_part",
                    description=part.description,
                    affected_object_ids=(key,),
                    part_name=part.name,
                    part_dimensions=dict(part.dimensions),
                    part_position=tuple(part.position),
                    part_color=part.color,
                )
            )
        steps.append(
            AgentPlanStep(
                step_key="combine",
                kind="assembly_combine",
                description=assembly.assembly_description,
                depends_on=tuple(part_keys),
                affected_object_ids=tuple(part_keys),
                output_formats=tuple(output_formats),
            )
        )
        return AgentPlan(
            objective=objective,
            operation="generate",
            model_kind="assembly",
            modeling_strategy="assembly_combine",
            design_brief=brief,
            affected_objects=tuple(objects),
            steps=tuple(steps),
            confirmation_policy=confirmation,
            confirmation_reason=reason,
        )

    async def plan_modification(
        self,
        existing_code: str,
        objective: str,
        *,
        expected_base_revision_id: UUID,
        output_formats: tuple[str, ...] = ("step", "stl"),
    ) -> AgentPlan:
        modification = await self.requirements_modification(
            existing_code,
            objective,
        )
        return self.compose_modification(
            objective,
            modification,
            expected_base_revision_id=expected_base_revision_id,
            output_formats=output_formats,
        )

    async def requirements_modification(
        self,
        existing_code: str,
        objective: str,
    ) -> ModificationPlan:
        """Interpret a modification without generating or executing source."""
        return await self.planner.plan_modification(
            [{"role": "user", "content": objective}],
            existing_code,
        )

    def compose_modification(
        self,
        objective: str,
        modification: ModificationPlan,
        *,
        expected_base_revision_id: UUID,
        output_formats: tuple[str, ...] = ("step", "stl"),
    ) -> AgentPlan:
        brief = DesignBrief(
            intent_summary=modification.description,
            artifact_type="custom",
            functional_requirements=[
                *modification.new_features,
                *(
                    f"{name}={value:g} mm"
                    for name, value in modification.target_params.items()
                ),
            ],
            acceptance_criteria=["修改后代码可执行并导出请求的工程文件"],
        )
        return AgentPlan(
            objective=objective,
            operation="modify",
            model_kind="simple",
            modeling_strategy=modification.modification_type,
            design_brief=brief,
            expected_base_revision_id=expected_base_revision_id,
            affected_objects=(
                AffectedObject(
                    object_id="part-main",
                    object_type="part",
                    label="当前模型",
                    change="modify",
                ),
            ),
            steps=(
                AgentPlanStep(
                    step_key="modify-main",
                    kind="modify",
                    description=modification.description,
                    affected_object_ids=("part-main",),
                    output_formats=tuple(output_formats),
                ),
            ),
            confirmation_policy=ConfirmationPolicy.REQUIRED,
            confirmation_reason="修改现有工程版本前需要确认计划。",
        )
