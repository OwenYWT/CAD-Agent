"""Execution backend boundary owned by the control plane."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from app.execution.contracts import ExecutionLeaseEnvelope, ExecutionResult, ExecutionSpec


@dataclass(frozen=True)
class MaterializedExecutionOutcome:
    """A wire-safe result plus process-local paths.

    Local paths are deliberately excluded from ``ExecutionResult`` so they can
    never be persisted or sent to a remote worker as part of the stable
    contract.
    """

    result: ExecutionResult
    files: Mapping[str, Path]
    work_dir: Path | None


class ExecutionBackend(Protocol):
    """Replaceable compute boundary.

    Implementations must propagate ``asyncio.CancelledError`` and stop the
    corresponding physical execution before returning control. Durable
    orchestration depends on this fencing contract when a Temporal activity is
    cancelled or superseded.
    """

    def runtime_snapshot(self): ...

    async def execute(
        self,
        spec: ExecutionSpec,
        lease: ExecutionLeaseEnvelope | None = None,
        materialized_inputs: Mapping[str, Path] | None = None,
        redacted_values: tuple[str, ...] = (),
    ) -> MaterializedExecutionOutcome: ...
