"""Bounded, classified repair policy and source generation for durable Agent runs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Callable

from app.agent.code_gen import CodeGenerator
from app.agent.failure_taxonomy import FixPath, classify
from app.llm import (
    get_last_chat_completion_provenance,
    reset_chat_completion_provenance,
)


@dataclass(frozen=True, slots=True)
class RepairDecision:
    repairable: bool
    failure_class: str
    strategy: str
    budget: int
    signature: str
    reason: str


@dataclass(frozen=True, slots=True)
class DurableRepairResult:
    source_code: str
    failure_class: str
    strategy: str
    provenance: dict[str, Any]


_FALLBACK_STRATEGIES = {
    "fillet_too_large": "remove_or_reduce_refinement",
    "shell_failed": "boolean_hollow",
    "empty_stack_boolean": "create_base_before_boolean",
    "cad_kernel": "simplify_failed_feature",
    "geometry_invalid": "rebuild_only_invalid_geometry",
}


def _failure_signature(
    *,
    category: str,
    error_code: str,
    failure_class: str,
    error_message: str,
) -> str:
    """Collapse volatile runtime details before applying the oscillation guard."""
    normalized = error_message.casefold()
    normalized = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b",
        "<id>",
        normalized,
    )
    normalized = re.sub(r"\b[0-9a-f]{32,64}\b", "<hash>", normalized)
    normalized = re.sub(r"\bline\s+\d+\b", "line <n>", normalized)
    normalized = " ".join(normalized.split())
    return hashlib.sha256(
        f"{category}\0{error_code}\0{failure_class}\0{normalized}".encode(
            "utf-8"
        )
    ).hexdigest()


def decide_repair(
    *,
    category: str,
    error_code: str,
    error_message: str,
    runtime_error_type: str | None,
    repair_count: int,
    seen_signatures: tuple[str, ...],
) -> RepairDecision:
    specific_failure = classify(None, error_message)
    generic_gate = (
        "InvalidCode"
        if category == "user_code"
        else "CADKernelError"
        if category == "cad_kernel"
        else "StaticAnalysis"
        if category == "validation" and error_code == "static_analysis_failed"
        else runtime_error_type
    )
    failure = (
        specific_failure
        if specific_failure.key != "unknown"
        else classify(runtime_error_type, error_message, gate=generic_gate)
    )
    signature = _failure_signature(
        category=category,
        error_code=error_code,
        failure_class=failure.key,
        error_message=error_message,
    )
    allowed_category = category in {"user_code", "cad_kernel"} or (
        category == "validation"
        and error_code
        in {"static_analysis_failed", "geometry_validation_failed"}
    )
    default_budget = 1 if category == "validation" else 2
    budget = min(
        failure.retry_budget
        if failure.retry_budget is not None
        else default_budget,
        2,
    )
    if not allowed_category or failure.fix_path is not FixPath.CODE:
        return RepairDecision(
            repairable=False,
            failure_class=failure.key,
            strategy="hard_stop",
            budget=0,
            signature=signature,
            reason="failure category cannot be repaired by changing CAD source",
        )
    if signature in seen_signatures:
        return RepairDecision(
            repairable=False,
            failure_class=failure.key,
            strategy="repeated_error_stop",
            budget=budget,
            signature=signature,
            reason="the same normalized failure already occurred after repair",
        )
    if repair_count >= budget:
        return RepairDecision(
            repairable=False,
            failure_class=failure.key,
            strategy="budget_exhausted",
            budget=budget,
            signature=signature,
            reason="classified repair budget is exhausted",
        )
    strategy = (
        _FALLBACK_STRATEGIES.get(failure.key, "minimal_targeted_fix")
        if repair_count > 0
        else "minimal_targeted_fix"
    )
    return RepairDecision(
        repairable=True,
        failure_class=failure.key,
        strategy=strategy,
        budget=budget,
        signature=signature,
        reason="classified source repair is allowed",
    )


class DurableRepairSourceGenerator:
    def __init__(
        self,
        *,
        code_generator: CodeGenerator | None = None,
        provenance_reader: Callable[[], dict[str, Any] | None] = (
            get_last_chat_completion_provenance
        ),
    ) -> None:
        self.code_generator = code_generator or CodeGenerator()
        self.provenance_reader = provenance_reader

    async def repair(
        self,
        *,
        source_code: str,
        failure: dict[str, Any],
        decision: RepairDecision,
    ) -> DurableRepairResult:
        if not decision.repairable:
            raise ValueError(decision.reason)
        reset_chat_completion_provenance()
        error = {
            "type": str(failure.get("runtime_error_type") or failure["error_code"]),
            "message": (
                f"{failure['error_message']}\n"
                f"Durable repair strategy: {decision.strategy}"
            ),
            "traceback": "",
            "gate": (
                "InvalidCode"
                if failure["category"] == "user_code"
                else "CADKernelError"
                if failure["category"] == "cad_kernel"
                else "StaticAnalysis"
            ),
        }
        repaired = await self.code_generator.fix_error(source_code, error)
        if not repaired.strip():
            raise ValueError("repair provider returned empty source")
        if repaired.strip() == source_code.strip():
            raise ValueError("repair provider returned unchanged source")
        provenance = self.provenance_reader()
        if provenance is None:
            raise RuntimeError("repair completed without provider provenance")
        return DurableRepairResult(
            source_code=repaired,
            failure_class=decision.failure_class,
            strategy=decision.strategy,
            provenance=provenance,
        )
