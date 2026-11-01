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
    AgentValidationPolicy,
    ConfirmationPolicy,
    GateMode,
    ValidationGatePolicy,
)
from app.agent.multi_step import BuildPhase, BuildPlan, BuildStep, PlanDecomposer
from app.agent.orchestrator import requires_design_confirmation
from app.agent.planner import Planner
from app.models.schemas import CADPlan, DesignBrief, ModificationPlan


_CUBE_INTENT_MARKERS = (
    "cube",
    "cubic box",
    "\u6b63\u65b9\u4f53",
    "\u7acb\u65b9\u4f53",
)


def _has_explicit_2d_output_intent(objective: str) -> bool:
    lowered = objective.lower()
    return any(marker in lowered for marker in ("dxf", "svg", "2d", "profile"))


def _has_explicit_cube_intent(objective: str) -> bool:
    lowered = objective.casefold()
    return any(marker in lowered for marker in _CUBE_INTENT_MARKERS)


def _cube_edge_from_acceptance(plan: CADPlan) -> float | None:
    acceptance = plan.design_brief.acceptance if plan.design_brief else None
    if acceptance is None:
        return None
    axis_values: dict[tuple[float, float, float], float] = {}
    for check in acceptance.checks:
        if check.kind != "overall_dimension" or check.comparison.op != "eq":
            continue
        axis = tuple(float(value) for value in check.scope.axis)
        if len(axis) != 3:
            continue
        axis_values[axis] = float(check.comparison.value)
    expected = [axis_values.get(axis) for axis in (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )]
    if any(value is None for value in expected):
        return None
    if len({float(value) for value in expected}) != 1:
        raise ValueError("cube acceptance requires equal X, Y and Z dimensions")
    return float(expected[0])


def _cube_edge_from_dimensions(plan: CADPlan) -> float | None:
    dimensions = {str(key).lower(): float(value) for key, value in plan.dimensions.items()}
    axis_values = [dimensions.get(key) for key in ("length", "width", "height")]
    if all(value is not None for value in axis_values):
        if len({float(value) for value in axis_values}) != 1:
            raise ValueError("cube requirements require equal length, width and height")
        return float(axis_values[0])
    candidates = [dimensions.get("length"), dimensions.get("width"), dimensions.get("height")]
    if dimensions.get("diameter") is not None:
        candidates.append(dimensions["diameter"])
    if dimensions.get("radius") is not None:
        candidates.append(dimensions["radius"] * 2)
    candidates = [float(value) for value in candidates if value is not None]
    if not candidates or len(set(candidates)) != 1:
        return None
    return candidates[0]


def _normalize_cube_requirements(plan: CADPlan, objective: str) -> CADPlan:
    if (
        not _has_explicit_cube_intent(objective)
        or plan.part_type in {"box", "profile_2d", "assembly"}
    ):
        return plan
    edge = _cube_edge_from_acceptance(plan) or _cube_edge_from_dimensions(plan)
    if edge is None or edge <= 0:
        raise ValueError("cube requirements do not contain one reliable edge dimension")
    canonical_feature = (
        f"base_box:length={edge:g},width={edge:g},height={edge:g}"
    )
    features = [
        canonical_feature,
        *[
            feature
            for feature in plan.features
            if "cylinder" not in feature.casefold()
        ],
    ]
    brief = ensure_design_brief(plan).model_copy(update={"artifact_type": "box"})
    return plan.model_copy(
        update={
            "part_type": "box",
            "dimensions": {
                "length": edge,
                "width": edge,
                "height": edge,
            },
            "features": features,
            "modeling_hint": "extrude_cut",
            "design_brief": brief,
        }
    )


def _normalize_generation_requirements(
    plan: CADPlan,
    objective: str,
    output_formats: tuple[str, ...],
) -> CADPlan:
    if plan.part_type != "profile_2d":
        return plan
    if not set(output_formats) & {"step", "stl"}:
        return plan
    if _has_explicit_2d_output_intent(objective):
        return plan
    dimensions = plan.dimensions
    if not {"length", "width", "height"}.issubset(dimensions):
        raise ValueError(
            "planner classified a solid-output request as profile_2d without length, width and height"
        )
    brief = ensure_design_brief(plan)
    solid_brief = brief.model_copy(update={"artifact_type": "box"})
    normalized = plan.model_copy(
        update={
            "part_type": "box",
            "modeling_hint": "extrude_cut",
            "design_brief": solid_brief,
        }
    )
    return _normalize_cube_requirements(normalized, objective)


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
        # DXF is one 2D document, not an incremental 3D feature tree. Feature
        # count must not send a profile through the FreeCAD solid backend.
        if plan.part_type == "profile_2d":
            return True
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
        if plan.part_type == "profile_2d":
            return "profile_2d"
        if plan.part_type == "assembly":
            return "assembly_combine"
        if plan.modeling_hint.strip():
            return plan.modeling_hint.strip()
        return "parametric"

    async def plan_generation(
        self,
        objective: str,
        *,
        output_formats: tuple[str, ...] = ("step", "stl"),
    ) -> AgentPlan:
        plan = await self.requirements_generation(
            objective,
            output_formats=output_formats,
        )
        decomposition = await self.decompose_generation(plan)
        return self.compose_generation(
            objective,
            plan,
            decomposition=decomposition,
            output_formats=output_formats,
        )

    async def requirements_generation(
        self,
        objective: str,
        *,
        output_formats: tuple[str, ...] = ("step", "stl"),
        require_acceptance: bool = False,
        acceptance_version=None,
        requirement_sources=None,
    ) -> CADPlan:
        """Run only requirements interpretation; never generate CAD source."""
        result = await self.planner.plan_new(
            [{"role": "user", "content": objective}],
            **({"require_acceptance": True} if require_acceptance else {}),
            **({'acceptance_version': acceptance_version} if acceptance_version else {}),
            **({'requirement_sources': requirement_sources} if requirement_sources is not None else {}),
        )
        if require_acceptance and result.part_type != 'profile_2d':
            if result.design_brief is None or result.design_brief.acceptance is None:
                raise ValueError('new solid requirements must retain their acceptance contract')
            if result.design_brief.acceptance.objective != objective:
                raise ValueError('acceptance contract objective differs from the submitted requirement')
        normalized = _normalize_generation_requirements(result, objective, output_formats)
        return _normalize_cube_requirements(normalized, objective)

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

    def compose_freecad_generation(
        self,
        objective: str,
        plan: CADPlan,
        *,
        output_formats: tuple[str, ...] = ("step", "stl"),
    ) -> AgentPlan:
        """Compose one atomic FreeCAD step from the complete typed requirements."""
        if plan.part_type in {"assembly", "profile_2d"}:
            raise ValueError(
                "FreeCAD MVP generation does not author assemblies or 2D profiles"
            )
        brief = ensure_design_brief(plan)
        confirmation, reason = self._confirmation(brief)
        return AgentPlan(
            objective=objective,
            operation="generate",
            model_kind="simple",
            modeling_strategy="freecad_operations",
            design_brief=brief,
            affected_objects=(
                AffectedObject(
                    object_id="part-main",
                    object_type="part",
                    label=brief.intent_summary or plan.description,
                    change="create",
                ),
            ),
            steps=(
                AgentPlanStep(
                    step_key="model-main",
                    kind="model",
                    description=plan.description,
                    affected_object_ids=("part-main",),
                    output_formats=tuple(output_formats),
                ),
            ),
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
            validation_policy=AgentValidationPolicy(
                bom=ValidationGatePolicy(
                    mode=GateMode.REQUIRED,
                    repair_budget=0,
                ),
            ),
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
        *, require_acceptance: bool = False, acceptance_version=None, verified_state=None, requirement_sources=None,
    ) -> ModificationPlan:
        """Interpret a modification without generating or executing source."""
        result = await self.planner.plan_modification(
            [{"role": "user", "content": objective}],
            existing_code,
            **({'require_acceptance':True} if require_acceptance else {}),
            **({'acceptance_version': acceptance_version} if acceptance_version else {}),
            **({'verified_state': verified_state} if verified_state is not None else {}),
            **({'requirement_sources': requirement_sources} if requirement_sources is not None else {}),
        )
        if require_acceptance and (result.acceptance is None or result.acceptance.objective != objective):
            raise ValueError('modification must retain its submitted acceptance contract')
        return result

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
            acceptance=modification.acceptance,
            open_questions=['必须确认：'+item for item in modification.acceptance.unresolved] if modification.acceptance else [],
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
