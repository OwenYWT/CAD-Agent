"""LLM-backed generator for the allowlisted FreeCAD operation contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable

from app.agent.durable_plan import AgentPlan
from app.config import make_llm_client, settings
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.operation_compiler import (
    compile_common_generation,
    compile_common_modification,
)
from app.llm import (
    get_last_chat_completion_provenance,
    reset_chat_completion_provenance,
)


_SYSTEM_PROMPT = """You are the operation planner for a headless FreeCAD 1.1.3 runtime.
Return exactly one JSON object matching freecad-operation-plan.v1. Never return Python,
markdown, prose, comments, expressions, or unsupported fields.

Allowed actions and args:
- document.inspect: {}
- sketch.create: {name, body?, plane: xy|xz|yz, offset_mm?, reversed?}
- sketch.add_geometry: {sketch, geometry}; geometry is line(start/end), circle(center/radius_mm),
  or rectangle(corner/width_mm/height_mm). Coordinates are numeric millimetres.
- sketch.add_constraint: {sketch, kind, first, second?, value_mm?}; kinds are horizontal,
  vertical, distance_x, distance_y, distance, radius, diameter, coincident, equal. References
  use geometry_index and optional point_position 1|2|3.
- feature.pad: {name, profile, length_mm, reversed?}
- feature.pocket: {name, profile, exactly one of length_mm or through_all:true, reversed?}
- feature.hole: {name, profile, diameter_mm, exactly one of depth_mm or through_all:true, reversed?}
- feature.fillet: {name, target, radius_mm, use_all_edges:true}
- feature.chamfer: {name, target, size_mm, use_all_edges:true}
- property.set: {object, property, value}
- document.export: {formats, basename}; this must be the single final operation.

Rules:
1. Use deterministic ASCII object names and unique deterministic lowercase op_id values.
2. A sketch used by a PartDesign feature must be fully constrained. Add dimensional and
   geometric constraints for every degree of freedom. Rectangle expands to geometry indexes
   0..3 in order; circle center is point_position 3.
3. For a new solid create a sketch before its feature. For a modification, only reference
   object names present in the supplied FreeCAD state.
4. Never persist or invent FaceN/EdgeN references. Fillet/chamfer only support all edges.
5. Preserve the requested dimensions exactly. The final export formats must exactly match
   the supplied required formats and must include fcstd.
6. Do not emit placeholders. If the request cannot be represented by this allowlist, return
   {"error":"unsupported: <specific reason>"} instead of an invalid plan.
"""


@dataclass(frozen=True, slots=True)
class FreeCADOperationGenerationResult:
    operation_plan: FreeCADOperationPlan
    source_code: str
    generator_kind: str
    provenance: dict[str, Any]


class FreeCADOperationGenerator:
    """Generate and repair typed operation JSON without exposing Python execution."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        provenance_reader: Callable[[], dict[str, Any] | None] = (
            get_last_chat_completion_provenance
        ),
    ) -> None:
        self._client = client
        self.provenance_reader = provenance_reader

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = make_llm_client()
        return self._client

    async def generate(
        self,
        *,
        plan: AgentPlan,
        requirements: dict[str, Any],
        base_state: dict[str, Any] | None,
        output_formats: tuple[str, ...],
    ) -> FreeCADOperationGenerationResult:
        required_formats = self._required_formats(output_formats)
        if plan.operation == "generate":
            compiled = compile_common_generation(
                requirements,
                output_formats=required_formats,
            )
        elif base_state is not None:
            compiled = compile_common_modification(
                requirements,
                base_state=base_state,
                output_formats=required_formats,
            )
        else:
            compiled = None
        if compiled is not None:
            source_code = compiled.model_dump_json()
            request_bytes = json.dumps(
                {
                    "operation": plan.operation,
                    "requirements": requirements,
                    "base_state": base_state,
                    "required_export_formats": list(required_formats),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return FreeCADOperationGenerationResult(
                operation_plan=compiled,
                source_code=source_code,
                generator_kind="freecad_operations_compiled",
                provenance={
                    "provider": "deterministic",
                    "model": "freecad-operation-compiler.v1",
                    "provider_response_id": None,
                    "request_hash": hashlib.sha256(request_bytes).hexdigest(),
                    "response_hash": hashlib.sha256(
                        source_code.encode("utf-8")
                    ).hexdigest(),
                    "finish_reason": "compiled",
                    "usage": {},
                },
            )
        payload = {
            "task": "generate" if plan.operation == "generate" else "modify",
            "agent_plan": {
                "objective": plan.objective,
                "operation": plan.operation,
                "model_kind": plan.model_kind,
                "modeling_strategy": plan.modeling_strategy,
                "steps": [
                    {
                        "step_key": step.step_key,
                        "kind": step.kind,
                        "description": step.description,
                        "depends_on": list(step.depends_on),
                    }
                    for step in plan.steps
                ],
            },
            "requirements": {
                key: value
                for key, value in requirements.items()
                if key not in {"design_brief", "manufacturing_profile"}
            },
            "base_freecad_state": base_state,
            "required_export_formats": list(self._required_formats(output_formats)),
        }
        return await self._complete(
            user_payload=payload,
            generator_kind="freecad_operations",
            output_formats=output_formats,
        )

    async def repair(
        self,
        *,
        source_code: str,
        failure: dict[str, Any],
        base_state: dict[str, Any] | None,
        output_formats: tuple[str, ...],
    ) -> FreeCADOperationGenerationResult:
        current = FreeCADOperationPlan.model_validate_json(source_code)
        payload = {
            "task": "repair",
            "current_operation_plan": current.model_dump(mode="json"),
            "execution_failure": failure,
            "base_freecad_state": base_state,
            "required_export_formats": list(self._required_formats(output_formats)),
            "instruction": (
                "Make the smallest operation-plan change that fixes the reported failure. "
                "Keep already-correct intent and deterministic names."
            ),
        }
        result = await self._complete(
            user_payload=payload,
            generator_kind="freecad_operation_repair",
            output_formats=output_formats,
        )
        if result.source_code == current.model_dump_json():
            raise ValueError("FreeCAD operation repair returned an unchanged plan")
        return result

    async def _complete(
        self,
        *,
        user_payload: dict[str, Any],
        generator_kind: str,
        output_formats: tuple[str, ...],
    ) -> FreeCADOperationGenerationResult:
        last_error: Exception | None = None
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        for attempt in range(2):
            reset_chat_completion_provenance()
            if attempt and last_error is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The prior JSON was rejected by the strict contract: "
                            f"{last_error}. Return a corrected complete JSON object."
                        ),
                    }
                )
            response = await self.client.chat.completions.create(
                model=settings.llm_model,
                max_tokens=settings.planner_max_tokens,
                temperature=0.0,
                timeout=max(settings.llm_timeout_s, 120.0),
                messages=messages,
                response_format={"type": "json_object"},
            )
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                last_error = ValueError("operation plan was truncated")
                continue
            content = getattr(choice.message, "content", None)
            if not isinstance(content, str) or not content.strip():
                last_error = ValueError("operation generator returned empty content")
                continue
            try:
                raw = json.loads(content)
                if isinstance(raw, dict) and raw.get("error"):
                    raise ValueError(str(raw["error"])[:1000])
                operation_plan = FreeCADOperationPlan.model_validate(raw)
                required = self._required_formats(output_formats)
                exported = tuple(
                    operation_plan.operations[-1].typed_args().formats
                )
                if set(exported) != set(required) or len(exported) != len(required):
                    raise ValueError(
                        "document.export formats do not match required formats"
                    )
            except Exception as exc:
                last_error = exc
                continue
            provenance = self.provenance_reader()
            if provenance is None:
                raise RuntimeError(
                    "FreeCAD operation generation completed without provider provenance"
                )
            source_code = operation_plan.model_dump_json()
            return FreeCADOperationGenerationResult(
                operation_plan=operation_plan,
                source_code=source_code,
                generator_kind=generator_kind,
                provenance=provenance,
            )
        raise ValueError("operation generator did not return a valid plan") from last_error

    @staticmethod
    def _required_formats(output_formats: tuple[str, ...]) -> tuple[str, ...]:
        requested = tuple(dict.fromkeys(str(item).lower() for item in output_formats))
        unsupported = set(requested) - {"step", "stl", "dxf", "fcstd"}
        if unsupported:
            raise ValueError(
                "FreeCAD does not support requested formats: "
                + ", ".join(sorted(unsupported))
            )
        return tuple(dict.fromkeys(("fcstd", *requested)))
