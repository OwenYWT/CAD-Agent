from dataclasses import dataclass, field

from app.models.schemas import DesignBrief, ManufacturingProfile


@dataclass
class ConversationContext:
    """Bounded UI conversation state; never owns task lifecycle or artifacts."""

    session_id: str
    messages: list[dict] = field(default_factory=list)
    current_code: str | None = None
    current_params: dict | None = None
    generation_count: int = 0
    assembly_parts: list[dict] | None = None
    current_design_brief: DesignBrief | None = None
    current_manufacturing_profile: ManufacturingProfile | None = None
