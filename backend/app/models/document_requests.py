"""Document operation wire input; serializer preserves legacy request hashes."""
from __future__ import annotations
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_serializer
from app.freecad.selection import SelectionContextV1
from app.models.native_modification import FreeCADStructuredModificationV1

class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["generate", "modify", "parameters.update", "native.update"]
    expected_base_revision_id: UUID
    expected_state_version: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=500)
    lease_token: UUID | None = None
    allow_rebase: bool = False
    objective: str = Field(default="", max_length=4000)
    modification: FreeCADStructuredModificationV1 | None = None
    selection_context: SelectionContextV1 | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_request_hash(self, handler):
        payload = handler(self)
        if self.selection_context is None:
            payload.pop("selection_context", None)
        return payload
