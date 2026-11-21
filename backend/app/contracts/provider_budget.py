"""Strong provider admission port, separate from best-effort usage observation."""
from contextvars import ContextVar
from typing import Any, Protocol

SUPPORTED_PROVIDER_BUDGET_CONTRACTS = frozenset({'provider-budget.v1'})
SUPPORTED_PROVIDER_RECEIPT_CONTRACTS = frozenset({'provider-receipt-recovery.v1'})
UNSENT_CONNECTION_FAILURES = frozenset({'connection.connect_tcp.failed', 'connection.start_tls.failed'})


class ProviderBudgetError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        self.code = code
        self.details = details
        super().__init__(message)


class ProviderBudgetPort(Protocol):
    async def prepare_request(self, params: dict, *, provider: str) -> dict: ...
    def logical_call_id(self) -> str: ...
    async def reserve_attempt(self, *, logical_call_id: str, attempt: int, request_digest: str,
                              provider: str, model: str) -> Any: ...
    async def claim_dispatch(self, reservation: Any) -> dict | None: ...
    async def record_receipt(self, reservation: Any, *, provenance: dict, response: dict) -> None: ...
    async def settle(self, reservation: Any, *, provenance: dict, response: dict) -> None: ...
    async def mark_unknown(self, reservation: Any, *, reason: str, diagnostics: dict | None = None) -> None: ...
    async def release_unsent(self, reservation: Any, *, diagnostics: dict | None = None) -> None: ...


provider_budget_context: ContextVar[ProviderBudgetPort | None] = ContextVar('provider_budget', default=None)
