"""Source-only adapter for durable Agent modeling steps."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app.agent.code_gen import CodeGenerator
from app.agent.durable_plan import AgentPlan, AgentPlanStep
from app.config import settings
from app.examples.retriever import ExampleRetriever
from app.llm import (
    get_last_chat_completion_provenance,
    reset_chat_completion_provenance,
)
from app.models.schemas import CADPlan, ModificationPlan


class Retriever(Protocol):
    async def find_similar(
        self,
        query: str,
        top_k: int = 3,
        **kwargs,
    ) -> list[dict]: ...


@dataclass(frozen=True, slots=True)
class SourceGenerationResult:
    source_code: str
    mode: str
    generator_kind: str
    provenance: dict[str, Any]


def _default_retriever() -> Retriever:
    if settings.example_retriever.strip().lower() == "vector":
        try:
            from app.examples.vector_retriever import VectorExampleRetriever

            return VectorExampleRetriever()
        except Exception:
            pass
    return ExampleRetriever()


class DurableModelingSourceGenerator:
    """Use proven retrieval/codegen components without owning execution state."""

    def __init__(
        self,
        *,
        retriever: Retriever | None = None,
        code_generator: CodeGenerator | None = None,
        provenance_reader: Callable[[], dict[str, Any] | None] = (
            get_last_chat_completion_provenance
        ),
    ) -> None:
        self.retriever = retriever or _default_retriever()
        self.code_generator = code_generator or CodeGenerator()
        self.provenance_reader = provenance_reader

    async def generate_step_source(
        self,
        *,
        plan: AgentPlan,
        step: AgentPlanStep,
        requirements: dict[str, Any],
        previous_source: str | None,
        step_index: int,
    ) -> SourceGenerationResult:
        reset_chat_completion_provenance()
        if plan.model_kind == "assembly":
            raise ValueError("assembly source generation uses the assembly workflow")
        if plan.operation == "modify":
            if step.kind != "modify" or step_index != 0:
                raise ValueError("modification plan must contain one modify step")
            existing_code = str(requirements.get("existing_code") or "")
            modification = ModificationPlan.model_validate(
                requirements["modification_plan"]
            )
            examples = await self.retriever.find_similar(plan.objective, top_k=2)
            source = await self.code_generator.modify(
                modification,
                existing_code,
                examples,
                [{"role": "user", "content": plan.objective}],
            )
            return self._result(source, step, "modify")

        cad_plan = CADPlan.model_validate(requirements)
        if plan.model_kind == "complex":
            accumulated = previous_source or "import cadquery as cq\nimport math\n\n"
            snippet = await self.code_generator.generate_step(
                step.description,
                accumulated,
                step_index,
                len(plan.steps),
                plan_context=self._plan_context(cad_plan),
            )
            return self._result(
                self._merge_step_source(accumulated, snippet),
                step,
                "generate_step",
            )

        if step_index != 0 or previous_source is not None:
            raise ValueError("simple/profile plan must generate exactly one source")
        examples = await self.retriever.find_similar(
            cad_plan.description,
            top_k=3,
            part_type=cad_plan.part_type,
            features=cad_plan.features,
            modeling_hint=cad_plan.modeling_hint or None,
        )
        if plan.model_kind == "profile_2d":
            source = await self.code_generator.generate_2d(
                cad_plan,
                examples,
                [{"role": "user", "content": plan.objective}],
            )
        else:
            source = await self.code_generator.generate(
                cad_plan,
                examples,
                [{"role": "user", "content": plan.objective}],
            )
        return self._result(
            source,
            step,
            "generate_2d" if plan.model_kind == "profile_2d" else "generate",
        )

    def _result(
        self,
        source: str,
        step: AgentPlanStep,
        generator_kind: str,
    ) -> SourceGenerationResult:
        if not source.strip():
            raise ValueError("code generator returned empty source")
        provenance = self.provenance_reader()
        if provenance is None:
            raise RuntimeError(
                "code generation completed without provider provenance"
            )
        return SourceGenerationResult(
            source_code=source,
            mode=self._mode(step),
            generator_kind=generator_kind,
            provenance=provenance,
        )

    @staticmethod
    def _mode(step: AgentPlanStep) -> str:
        return (
            "2d"
            if step.output_formats
            and set(step.output_formats).issubset({"dxf", "svg"})
            else "3d"
        )

    @staticmethod
    def _plan_context(plan: CADPlan) -> str:
        return (
            f"描述: {plan.description}\n"
            f"类型: {plan.part_type}\n"
            f"尺寸: {plan.dimensions}\n"
            f"特征: {plan.features}\n"
            f"约束: {plan.constraints}"
        )

    @staticmethod
    def _merge_step_source(accumulated: str, snippet: str) -> str:
        clean_lines = []
        for line in snippet.strip().splitlines():
            stripped = line.strip()
            if stripped.startswith("show_object"):
                continue
            if stripped.startswith("import ") or stripped.startswith("from "):
                continue
            clean_lines.append(line)
        clean = "\n".join(clean_lines).strip()
        if not clean:
            raise ValueError("code generator returned an empty modeling step")
        return accumulated.rstrip() + "\n\n" + clean + "\n"
