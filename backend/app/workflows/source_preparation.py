"""Prepare executable MCAD source inside a durable workflow activity."""
from __future__ import annotations

from typing import Any

from app.agent.design_brief import (
    ensure_design_brief,
)
from app.agent.orchestrator import (
    Orchestrator,
    requires_design_confirmation,
)
from app.models.schemas import ManufacturingProfile
from app.models.workflow_requests import (McadExecutionRequest, McadOutputRequest, McadSourcePreparationRequest)


_MEDIA_TYPES = {
    "step": "model/step",
    "stl": "model/stl",
    "dxf": "image/vnd.dxf",
    "svg": "image/svg+xml",
}


def _outputs(formats: tuple[str, ...]) -> tuple[McadOutputRequest, ...]:
    return tuple(
        McadOutputRequest(name=name, media_type=_MEDIA_TYPES[name])
        for name in formats
    )


class SourcePreparer:
    """LLM planning/code generation without invoking the CAD executor."""

    def __init__(self, orchestrator: Orchestrator):
        self.orchestrator = orchestrator

    async def prepare(
        self,
        request: McadSourcePreparationRequest,
    ) -> dict[str, Any]:
        if request.operation == "modify":
            return await self._prepare_modification(request)
        return await self._prepare_generation(request)

    async def _prepare_modification(
        self,
        request: McadSourcePreparationRequest,
    ) -> dict[str, Any]:
        existing_code = request.existing_code or ""
        plan = await self.orchestrator.planner.plan_modification(
            [{"role": "user", "content": request.prompt}],
            existing_code,
        )
        examples = await self.orchestrator.retriever.find_similar(
            request.prompt,
            top_k=3,
        )
        source_code = await self.orchestrator.code_gen.modify(
            plan,
            existing_code,
            examples,
            [{"role": "user", "content": request.prompt}],
        )
        is_2d = "ezdxf" in existing_code or "result.dxf" in existing_code
        formats = ("dxf",) if is_2d else request.output_formats
        execution = McadExecutionRequest(
            step_key="model",
            kind="mcad_model",
            operation="modify",
            mode="2d" if is_2d else "3d",
            source_code=source_code,
            outputs=_outputs(formats),
        )
        return {
            "needs_confirmation": False,
            "execution": execution.model_dump(mode="json"),
            "source_code": source_code,
            "modification_plan": plan.model_dump(mode="json"),
            "output_formats": list(formats),
        }

    async def _prepare_generation(
        self,
        request: McadSourcePreparationRequest,
    ) -> dict[str, Any]:
        profile = (
            ManufacturingProfile.model_validate(
                request.manufacturing_profile
            )
            if request.manufacturing_profile
            else None
        )
        force_2d = set(request.output_formats).issubset({"dxf", "svg"})
        requested_prompt = request.prompt
        if force_2d:
            requested_prompt = (
                "只生成 1:1 的二维工程图，不生成三维模型。"
                "将需求规划为 profile_2d，并按请求格式导出。\n"
                + request.prompt
            )
        planner_prompt = self.orchestrator._prompt_with_manufacturing_profile(
            requested_prompt,
            profile,
        )
        plan = await self.orchestrator.planner.plan_new(
            [{"role": "user", "content": planner_prompt}]
        )
        plan.manufacturing_profile = profile
        if force_2d:
            # The public output contract is authoritative. A planner cannot
            # silently turn a DXF-only request into an incompatible 3D job.
            plan.part_type = "profile_2d"
        design_brief = ensure_design_brief(plan)
        self.orchestrator._apply_manufacturing_profile_to_brief(
            design_brief,
            profile,
        )
        plan_payload = plan.model_dump(mode="json")
        brief_payload = design_brief.model_dump(mode="json")
        if requires_design_confirmation(design_brief):
            return {
                "needs_confirmation": True,
                "plan": plan_payload,
                "design_brief": brief_payload,
                "manufacturing_profile": (
                    profile.model_dump(mode="json") if profile else None
                ),
            }

        examples = await self.orchestrator.retriever.find_similar(
            plan.description,
            top_k=3,
            part_type=plan.part_type,
            features=plan.features,
            modeling_hint=plan.modeling_hint or None,
        )
        if plan.part_type == "profile_2d":
            source_code = await self.orchestrator.code_gen.generate_2d(
                plan,
                examples,
                [{"role": "user", "content": requested_prompt}],
            )
            mode = "2d"
            formats = ("dxf",)
        elif plan.part_type == "assembly":
            source_code = await self.orchestrator.code_gen.generate_assembly(
                plan,
                examples,
                [{"role": "user", "content": request.prompt}],
            )
            mode = "3d"
            formats = request.output_formats
        else:
            source_code = await self.orchestrator.code_gen.generate(
                plan,
                examples,
                [{"role": "user", "content": request.prompt}],
                extra_context=self.orchestrator._lookup_standard_parts(plan),
            )
            mode = "3d"
            formats = request.output_formats

        execution = McadExecutionRequest(
            step_key="model",
            kind="mcad_model",
            operation="generate",
            mode=mode,
            source_code=source_code,
            outputs=_outputs(formats),
        )
        return {
            "needs_confirmation": False,
            "execution": execution.model_dump(mode="json"),
            "source_code": source_code,
            "plan": plan_payload,
            "design_brief": brief_payload,
            "manufacturing_profile": (
                profile.model_dump(mode="json") if profile else None
            ),
            "output_formats": list(formats),
        }
