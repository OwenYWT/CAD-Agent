"""Provider observation port. A sink receives facts, never model content."""
from typing import Any, Protocol


class UsageSink(Protocol):
    async def start_call(self, *, provider: str, model: str, request_hash: str, attempt: int) -> Any: ...
    async def finish_call(self, handle: Any, *, duration_ms: int, provenance=None, error=None) -> None: ...
