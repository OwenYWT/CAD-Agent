"""Request/task-local control-plane principal context."""
from __future__ import annotations

from contextvars import ContextVar

from app.domain.identity import PrincipalContext, local_anonymous_principal


_CURRENT_PRINCIPAL: ContextVar[PrincipalContext | None] = ContextVar(
    "cad_agent_current_principal",
    default=None,
)


def bind_principal(context: PrincipalContext) -> None:
    _CURRENT_PRINCIPAL.set(context)


def current_principal() -> PrincipalContext:
    return _CURRENT_PRINCIPAL.get() or local_anonymous_principal()
