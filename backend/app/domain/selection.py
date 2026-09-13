"""Wire identity for an explicitly selected native modification target."""
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.topology.contracts import FreeCADTopologySelector


class SelectionContextV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision_id: UUID
    state_version: int = Field(ge=0, strict=True)
    feature_ids: tuple[UUID, ...] = Field(min_length=1, max_length=8)
    topology_selector: FreeCADTopologySelector | None = None

    @model_validator(mode="after")
    def unique_targets(self):
        if len(set(self.feature_ids)) != len(self.feature_ids):
            raise ValueError("选择目标不能重复")
        if self.topology_selector and (
            self.topology_selector.revision_id != self.revision_id or len(self.feature_ids) != 1
        ):
            raise ValueError("子元素选择必须属于同一版本中的一个特征")
        return self
