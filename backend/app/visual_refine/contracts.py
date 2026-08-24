"""Data contracts for the VLM-in-the-loop refinement package.

These are pure pydantic models: no IO, no provider, no rendering. Keeping them
in their own module is what lets the loop, the critic, the patcher and the
callers in ``app.agent`` / ``app.validation`` depend on a shape rather than on
each other.

One rule is enforced here rather than left to a prompt: **the overall verdict is
computed from the individual checks, never taken from the model's own summary.**
A vision model that lists a blocking violation and then writes
``"is_match": true`` is a real and frequent failure mode; deriving the verdict
makes it structurally impossible.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Verdict = Literal["satisfied", "violated", "not_visible"]
Severity = Literal["blocking", "major", "minor"]
Origin = Literal["measured", "observed"]

# Weight per severity when scoring a candidate. Blocking failures dominate so a
# candidate that fuses a floating body always outranks one that merely rounds a
# fillet differently.
_SEVERITY_WEIGHT: dict[str, float] = {
    "blocking": 6.0,
    "major": 2.5,
    "minor": 1.0,
}


class RequirementCheck(BaseModel):
    """One requirement, judged either by measurement or by looking."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=120)
    requirement: str = Field(min_length=1, max_length=600)
    verdict: Verdict
    observed: str = Field(default="", max_length=800)
    severity: Severity = "major"
    # "measured" comes from mesh geometry and is authoritative. "observed" comes
    # from the vision model and can be overruled by a measurement.
    origin: Origin = "observed"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @property
    def is_failure(self) -> bool:
        return self.verdict == "violated"

    @property
    def weight(self) -> float:
        return _SEVERITY_WEIGHT.get(self.severity, 1.0)


class VisualCritique(BaseModel):
    """The full judgement of one candidate model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: tuple[RequirementCheck, ...] = ()
    issues: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()
    summary: str = Field(default="", max_length=2000)
    # Reported by the vision model for its own reading. It gates *pass*, never
    # *fail*: a low-confidence pass is not a pass, but a confident-sounding
    # model cannot talk its way past a violated check.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # Set when the critique could not be produced at all (provider down, parse
    # failure). Indeterminate must never read as a pass anywhere downstream.
    indeterminate: bool = False
    indeterminate_reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _indeterminate_carries_a_reason(self) -> "VisualCritique":
        if self.indeterminate and not self.indeterminate_reason:
            raise ValueError("an indeterminate critique must carry a reason")
        return self

    @property
    def blocking_failures(self) -> tuple[RequirementCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if check.is_failure and check.severity in {"blocking", "major"}
        )

    @property
    def failures(self) -> tuple[RequirementCheck, ...]:
        return tuple(check for check in self.checks if check.is_failure)

    @property
    def is_match(self) -> bool | None:
        """Tri-state verdict. ``None`` means indeterminate, never a pass."""
        if self.indeterminate:
            return None
        if self.blocking_failures:
            return False
        # A model that saw nothing it was asked about has not confirmed anything.
        if not self.checks:
            return None
        if self.confidence < 0.7:
            return False
        return True

    def score(self) -> float:
        """Weighted satisfaction in 0..1, used to rank candidates.

        ``not_visible`` scores as a half credit: it is neither evidence of a
        defect nor confirmation, and a candidate that at least shows its features
        should outrank one that hides them.
        """
        if not self.checks:
            return 0.0
        earned = 0.0
        possible = 0.0
        for check in self.checks:
            possible += check.weight
            if check.verdict == "satisfied":
                earned += check.weight
            elif check.verdict == "not_visible":
                earned += check.weight * 0.5
        return earned / possible if possible else 0.0

    def repair_brief(self) -> str:
        """The defect list handed to the patcher, worst first."""
        ordered = sorted(
            self.failures,
            key=lambda check: (-check.weight, check.key),
        )
        lines: list[str] = []
        for check in ordered:
            marker = "BLOCKING" if check.severity == "blocking" else check.severity.upper()
            lines.append(
                f"[{marker}] {check.requirement}\n"
                f"    observed: {check.observed or 'requirement not met'}"
            )
        for issue in self.issues:
            if issue not in lines:
                lines.append(f"[NOTE] {issue}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RefinementAttempt(BaseModel):
    """One turn of the render -> critique -> patch loop, kept for audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    score: float = Field(ge=0.0, le=1.0)
    verdict: Literal["passed", "failed", "indeterminate", "build_failed"]
    critique: VisualCritique | None = None
    geometry: dict[str, Any] = Field(default_factory=dict)
    render_files: tuple[str, ...] = ()
    error: str = Field(default="", max_length=2000)


class RefinementOutcome(BaseModel):
    """What the loop returns: the best candidate it could prove, plus the trail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "improved", "unchanged", "indeterminate", "failed"]
    source_code: str
    critique: VisualCritique | None = None
    geometry: dict[str, Any] = Field(default_factory=dict)
    attempts: tuple[RefinementAttempt, ...] = ()
    render_paths: tuple[str, ...] = ()
    # True when the loop replaced the code it was given. Callers use this to
    # decide whether to re-execute and re-publish artifacts.
    changed: bool = False

    @property
    def iterations(self) -> int:
        return len(self.attempts)

    def summary_line(self) -> str:
        score = f"{self.critique.score():.2f}" if self.critique else "n/a"
        return (
            f"visual refinement {self.status} after {self.iterations} "
            f"iteration(s), score {score}"
        )
