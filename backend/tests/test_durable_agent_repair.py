"""Classified durable repair policy and provider adapter tests."""
from __future__ import annotations

import pytest

from app.agent.durable_repair import DurableRepairSourceGenerator, decide_repair


@pytest.mark.parametrize(
    "category,error_code",
    [
        ("infrastructure", "runtime_unavailable"),
        ("timeout", "execution_timeout"),
        ("resource", "execution_oom"),
        ("artifact", "artifact_rejected"),
        ("cancellation", "execution_cancelled"),
        ("provider", "provider_unavailable"),
        ("lease", "stale_lease"),
        ("database", "database_unavailable"),
        ("object_store", "object_store_unavailable"),
    ],
)
def test_non_code_failures_are_hard_stops(category, error_code):
    decision = decide_repair(
        category=category,
        error_code=error_code,
        error_message="controlled failure",
        runtime_error_type=None,
        repair_count=0,
        seen_signatures=(),
    )
    assert decision.repairable is False
    assert decision.strategy == "hard_stop"


def test_user_code_and_kernel_failures_are_bounded_and_repeat_stops():
    first = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message="SyntaxError on line 3",
        runtime_error_type="SyntaxError",
        repair_count=0,
        seen_signatures=(),
    )
    repeated = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message="SyntaxError on line 3",
        runtime_error_type="SyntaxError",
        repair_count=1,
        seen_signatures=(first.signature,),
    )
    kernel = decide_repair(
        category="cad_kernel",
        error_code="cad_execution_failed",
        error_message="BRep_API: not done",
        runtime_error_type="CADKernelError",
        repair_count=1,
        seen_signatures=(),
    )
    assert first.repairable is True
    assert first.budget == 2
    assert repeated.repairable is False
    assert repeated.strategy == "repeated_error_stop"
    assert kernel.repairable is True
    assert kernel.strategy == "remove_or_reduce_refinement"


def test_failure_signature_ignores_attempt_ids_hashes_and_line_numbers():
    first = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message=(
            "SyntaxError line 3 attempt "
            "6b0deee9-e5eb-4d10-b57b-c12ac076c9a4 "
            + "a" * 64
        ),
        runtime_error_type="SyntaxError",
        repair_count=0,
        seen_signatures=(),
    )
    repeated = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message=(
            "SyntaxError line 7 attempt "
            "97971586-62b4-45c1-bbb4-50cdcfaf5ec8 "
            + "b" * 64
        ),
        runtime_error_type="SyntaxError",
        repair_count=1,
        seen_signatures=(first.signature,),
    )
    assert repeated.repairable is False
    assert repeated.strategy == "repeated_error_stop"


class CodeGeneratorStub:
    calls = 0

    async def fix_error(self, source, error):
        self.calls += 1
        return source.replace("box(", "box(20, ")


@pytest.mark.asyncio
async def test_repair_adapter_records_provenance_and_rejects_unchanged_source():
    codegen = CodeGeneratorStub()
    service = DurableRepairSourceGenerator(
        code_generator=codegen,
        provenance_reader=lambda: {
            "provider": "controlled-provider",
            "model": "controlled-model",
            "provider_response_id": "repair-1",
            "request_hash": "a" * 64,
            "response_hash": "b" * 64,
            "finish_reason": "stop",
            "usage": {},
        },
    )
    decision = decide_repair(
        category="user_code",
        error_code="user_code_failed",
        error_message="TypeError",
        runtime_error_type="TypeError",
        repair_count=0,
        seen_signatures=(),
    )
    result = await service.repair(
        source_code="result = box(10, 4)",
        failure={
            "category": "user_code",
            "error_code": "user_code_failed",
            "error_message": "TypeError",
            "runtime_error_type": "TypeError",
        },
        decision=decision,
    )
    assert result.source_code != "result = box(10, 4)"
    assert result.provenance["provider_response_id"] == "repair-1"
    assert codegen.calls == 1
