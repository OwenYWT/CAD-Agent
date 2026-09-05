"""Public response models for the capability catalog."""

from typing import Literal

from pydantic import BaseModel, Field


Maturity = Literal["stable", "beta", "experimental"]
RiskLevel = Literal["read_only", "compute", "external_write", "physical_action"]


class CapabilityAction(BaseModel):
    id: str
    name: str
    mode: RiskLevel
    requires_confirmation: bool = False
    available: bool = True
    blocked_reason: str | None = None


class CapabilityDependency(BaseModel):
    id: str
    label: str
    kind: str
    required: bool
    available: bool | None  # None: checked at action time, not known missing
    detail: str


class UpstreamProvenance(BaseModel):
    version: str
    commit: str
    url: str


class CapabilityManifest(BaseModel):
    id: str
    name: str
    group: str
    summary: str
    maturity: Maturity
    risk_level: RiskLevel
    actions: list[CapabilityAction] = Field(default_factory=list)
    accepts: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    dependencies: list[CapabilityDependency] = Field(default_factory=list)
    available: bool
    blocked_reasons: list[str] = Field(default_factory=list)
    upstream: UpstreamProvenance
