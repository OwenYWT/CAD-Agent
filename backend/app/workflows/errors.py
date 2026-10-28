"""Error classification shared by model handlers and activity wrappers."""
from __future__ import annotations
from temporalio.exceptions import ApplicationError
from app.services.public_errors import public_generation_error
from app.freecad.contracts import FreeCADPlanningError
from app.freecad.state_contract import ParameterStateError
from app.llm import is_nonretryable_provider_error
from app.contracts.acceptance_v2 import RequirementBindingError
from app.contracts.provider_budget import ProviderBudgetError

def planning_error(exc: Exception) -> ApplicationError:
    if isinstance(exc, ApplicationError):
        return exc
    if isinstance(exc, (ParameterStateError, FreeCADPlanningError, RequirementBindingError, ProviderBudgetError)):
        return ApplicationError(
            exc.message if isinstance(exc, RequirementBindingError) else str(exc),
            type=exc.code,
            non_retryable=True,
        )
    public = public_generation_error(exc)
    non_retryable = (
        is_nonretryable_provider_error(exc)
        or isinstance(exc, (RuntimeError, ValueError, KeyError))
    )
    return ApplicationError(
        public["message"],
        type=public["type"][:200],
        non_retryable=non_retryable,
    )
