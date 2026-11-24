"""Compatibility import for HTTP error presentation."""
from uuid import uuid4

from fastapi.responses import JSONResponse

from app.domain.revisions import StaleBaseRevision
from app.models.schemas import GenerateResponse
from app.services.run_state import IdempotencyConflict
from app.services.public_errors import *  # noqa: F403


def submission_conflict(exc: StaleBaseRevision | IdempotencyConflict, request_id: str) -> GenerateResponse:
    kind = "RevisionConflict" if isinstance(exc, StaleBaseRevision) else "IdempotencyConflict"
    return GenerateResponse(request_id=request_id, success=False, error={"type": kind, "message": str(exc)})


def submission_conflict_response(exc: StaleBaseRevision | IdempotencyConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content=submission_conflict(exc, str(uuid4())).model_dump(mode="json"))
