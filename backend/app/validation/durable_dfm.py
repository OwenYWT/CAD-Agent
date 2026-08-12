"""Strict deterministic DFM evidence returned by the isolated worker."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DFMViolationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=120)
    severity: Literal["critical", "warning", "info"]
    actual_value: float | None = None
    message: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(default="", max_length=2000)
    source: Literal["geometric", "heuristic"]


class DurableDFMReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["durable-dfm-report.v1"]
    outcome: Literal["passed", "failed", "indeterminate"]
    process: str = Field(min_length=1, max_length=120)
    material: str = Field(min_length=1, max_length=240)
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metrics: dict[str, float | int | bool]
    evaluated_rule_ids: tuple[str, ...] = ()
    unevaluated_rule_ids: tuple[str, ...] = ()
    violations: tuple[DFMViolationEvidence, ...] = ()
    issues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def outcome_matches_findings(self):
        confirmed = any(item.severity != "info" for item in self.violations)
        if self.outcome == "passed" and (confirmed or self.issues):
            raise ValueError("passed DFM evidence cannot contain blocking findings")
        if self.outcome == "failed" and not confirmed:
            raise ValueError("failed DFM evidence requires a confirmed finding")
        if self.outcome == "indeterminate" and not self.issues:
            raise ValueError("indeterminate DFM evidence requires an issue")
        return self

    def durable_evidence(
        self,
        *,
        runtime_provenance: dict[str, Any] | None,
        policy_object: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "runtime_provenance": runtime_provenance,
            "policy_object": policy_object,
        }


def indeterminate_dfm_report(
    *,
    process: str,
    material: str,
    policy_hash: str,
    issue: str,
) -> DurableDFMReport:
    return DurableDFMReport(
        schema_version="durable-dfm-report.v1",
        outcome="indeterminate",
        process=process,
        material=material,
        policy_hash=policy_hash,
        metrics={},
        issues=(issue,),
    )
