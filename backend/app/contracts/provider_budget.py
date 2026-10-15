"""Strong provider admission port, separate from best-effort usage observation."""
from contextvars import ContextVar
from typing import Any, Protocol

SUPPORTED_PROVIDER_BUDGET_CONTRACTS = frozenset({'provider-budget.v1'})


class ProviderBudgetError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ProviderBudgetPort(Protocol):
    async def prepare_request(self, params: dict, *, provider: str) -> dict: ...
    def logical_call_id(self) -> str: ...
    async def reserve_attempt(self, *, logical_call_id: str, attempt: int, request_digest: str,
                              provider: str, model: str) -> Any: ...
    async def claim_dispatch(self, reservation: Any) -> dict | None: ...
    async def settle(self, reservation: Any, *, provenance: dict, response: dict) -> None: ...
    async def mark_unknown(self, reservation: Any, *, reason: str) -> None: ...


provider_budget_context: ContextVar[ProviderBudgetPort | None] = ContextVar('provider_budget', default=None)
