"""Strict visual-gate contracts and the external vision-provider boundary."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import make_llm_client, settings
from app.llm import (
    get_last_chat_completion_provenance,
    reset_chat_completion_provenance,
)


class VisualRenderEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    view: Literal["front", "right", "top", "isometric"]
    filename: str = Field(min_length=1, max_length=255)
    object_key: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)


class VisualJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    is_match: bool
    confidence: float = Field(ge=0, le=1)
    issues: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def match_has_sufficient_confidence(self):
        if self.is_match and self.confidence < 0.7:
            raise ValueError("a visual pass requires confidence >= 0.7")
        return self


class VisualProviderProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=240)
    provider_response_id: str | None = Field(default=None, max_length=500)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    finish_reason: str | None = Field(default=None, max_length=120)
    usage: dict[str, Any] = Field(default_factory=dict)


class DurableVisualReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["durable-visual-report.v1"]
    outcome: Literal["passed", "failed", "indeterminate"]
    renders: tuple[VisualRenderEvidence, ...] = ()
    judgment: VisualJudgment | None = None
    issues: tuple[str, ...] = ()
    runtime_provenance: dict[str, Any] | None = None
    provider_provenance: VisualProviderProvenance | None = None
    # Per-requirement verdicts behind the judgment, from app.visual_refine. Kept
    # in the evidence so a stored pass or fail can be audited check by check
    # rather than re-argued from a one-line summary. Older evidence predates the
    # field and simply carries None.
    critique: dict[str, Any] | None = None
    geometry: dict[str, Any] | None = None

    @model_validator(mode="after")
    def outcome_matches_judgment(self):
        if self.outcome == "indeterminate":
            if self.judgment is not None or not self.issues:
                raise ValueError("indeterminate visual evidence requires an issue")
            return self
        if len(self.renders) != 4 or self.judgment is None:
            raise ValueError("visual pass/fail requires four renders and a judgment")
        if self.provider_provenance is None:
            raise ValueError("visual judgment requires provider provenance")
        if (self.outcome == "passed") != self.judgment.is_match:
            raise ValueError("visual outcome disagrees with judgment")
        return self

    def durable_evidence(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DurableVisualValidator:
    """Call only the vision provider; CAD rendering stays in the sandbox.

    The judgement itself is delegated to :mod:`app.visual_refine`, so the durable
    gate and the in-process agent inspect models with the same requirement set,
    the same measured-versus-observed split and the same derived verdict. What
    stays here is the durable contract: four renders, provider provenance, and a
    tri-state outcome that never launders a provider failure into a pass.
    """

    def __init__(
        self,
        *,
        client=None,
        code_generator=None,
        provenance_reader: Callable[[], dict[str, Any] | None] = (
            get_last_chat_completion_provenance
        ),
    ) -> None:
        self._client = client
        self._code_generator = code_generator
        self.provenance_reader = provenance_reader
        self._last_critique: dict[str, Any] | None = None

    @property
    def client(self):
        if self._client is None:
            self._client = make_llm_client()
        return self._client

    def _spec(self, objective: str, design_brief: dict[str, Any]):
        from app.visual_refine.spec import build_spec

        return build_spec(objective=objective, design_brief=design_brief or {})

    async def judge(
        self,
        *,
        objective: str,
        design_brief: dict[str, Any],
        render_paths: tuple[Path, ...],
        geometry: dict[str, Any] | None = None,
        supplementary_render_paths: tuple[Path, ...] = (),
    ) -> tuple[VisualJudgment, dict[str, Any]]:
        """Judge four durable renders, plus any supplementary section views.

        ``geometry`` carries the worker's measured facts. When present they are
        handed to the critic as authoritative, which is what stops the model
        re-deciding a dimension or a hole count by eye.
        """
        if len(render_paths) != 4 or any(not path.is_file() for path in render_paths):
            raise ValueError("visual judgment requires four materialized renders")

        from app.visual_refine.critic import VisionCritic
        from app.visual_refine.facts import GeometryFacts

        spec = self._spec(objective, design_brief)
        facts: GeometryFacts | None = None
        measured: tuple[Any, ...] = ()
        if geometry:
            facts = _facts_from_payload(geometry)
            if facts is not None:
                measured = spec.measured_checks(facts)

        critic = VisionCritic(
            client=self.client,
            model=settings.effective_vision_model,
            max_tokens=settings.vision_critic_max_tokens,
        )
        reset_chat_completion_provenance()
        usable = list(render_paths) + [
            path for path in supplementary_render_paths if path.is_file()
        ]
        # judge() rather than critique(): the durable gate must tell a provider
        # outage apart from a genuine mismatch, so the exception has to reach
        # report() to become an indeterminate outcome.
        critique = await critic.judge(
            spec=spec,
            render_paths=usable,
            facts=facts,
            measured_checks=measured,
        )
        provenance = self.provenance_reader()
        if provenance is None:
            raise RuntimeError("vision judgment completed without provenance")

        self._last_critique = critique.as_dict()
        match = critique.is_match
        if match is None:
            raise ValueError("vision judgment was indeterminate")
        # VisualJudgment refuses a pass under 0.7 confidence, and the derived
        # verdict already applies the same floor, so the two cannot disagree.
        return (
            VisualJudgment(
                is_match=match,
                confidence=critique.confidence,
                issues=tuple(
                    check.observed or check.requirement
                    for check in critique.failures
                )
                + critique.issues,
                suggestions=critique.suggestions,
            ),
            provenance,
        )

    async def report(
        self,
        *,
        objective: str,
        design_brief: dict[str, Any],
        render_paths: tuple[Path, ...],
        renders: tuple[VisualRenderEvidence, ...],
        runtime_provenance: dict[str, Any] | None,
        geometry: dict[str, Any] | None = None,
        supplementary_render_paths: tuple[Path, ...] = (),
    ) -> DurableVisualReport:
        """Return a strict tri-state report without disguising provider failure."""
        self._last_critique = None
        try:
            judgment, provenance = await self.judge(
                objective=objective,
                design_brief=design_brief,
                render_paths=render_paths,
                geometry=geometry,
                supplementary_render_paths=supplementary_render_paths,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return indeterminate_visual_report(
                issue=f"vision_provider_unavailable:{type(exc).__name__}",
                renders=renders,
                runtime_provenance=runtime_provenance,
            )
        return DurableVisualReport(
            schema_version="durable-visual-report.v1",
            outcome="passed" if judgment.is_match else "failed",
            renders=renders,
            judgment=judgment,
            issues=judgment.issues,
            runtime_provenance=runtime_provenance,
            provider_provenance=provenance,
            critique=self._last_critique,
            geometry=geometry or None,
        )

    async def repair(
        self,
        *,
        source_code: str,
        issues: tuple[str, ...],
        suggestions: tuple[str, ...],
        objective: str = "",
        design_brief: dict[str, Any] | None = None,
        render_paths: tuple[Path, ...] = (),
        geometry: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Repair the source, showing the model the renders that failed.

        With renders in hand this runs the multimodal patcher: the model sees
        what its code actually built while fixing it. Without them -- an older
        payload, or a render that never materialised -- it falls back to the
        text-only repair prompt rather than refusing to repair at all.
        """
        reset_chat_completion_provenance()
        usable = tuple(path for path in render_paths if Path(path).is_file())
        if usable:
            repaired = await self._repair_with_renders(
                source_code=source_code,
                issues=issues,
                suggestions=suggestions,
                objective=objective,
                design_brief=design_brief or {},
                render_paths=usable,
                geometry=geometry,
            )
        else:
            repaired = await self.repair_generator.fix_visual_issues(
                source_code,
                list(issues),
                list(suggestions),
            )
        if not repaired.strip() or repaired.strip() == source_code.strip():
            raise ValueError("visual repair returned empty or unchanged source")
        provenance = self.provenance_reader()
        if provenance is None:
            raise RuntimeError("visual repair completed without provenance")
        return repaired, provenance

    async def _repair_with_renders(
        self,
        *,
        source_code: str,
        issues: tuple[str, ...],
        suggestions: tuple[str, ...],
        objective: str,
        design_brief: dict[str, Any],
        render_paths: tuple[Path, ...],
        geometry: dict[str, Any] | None,
    ) -> str:
        from app.visual_refine.contracts import RequirementCheck, VisualCritique
        from app.visual_refine.patcher import VisionPatcher

        spec = self._spec(objective, design_brief)
        critique = VisualCritique(
            checks=tuple(
                RequirementCheck(
                    key=f"visual.issue.{index}",
                    requirement=issue,
                    verdict="violated",
                    observed=issue,
                    severity="major",
                )
                for index, issue in enumerate(issues)
            ),
            issues=issues,
            suggestions=suggestions,
            confidence=0.0,
        )
        patcher = VisionPatcher(
            client=self.client,
            model=settings.effective_vision_model,
            max_tokens=settings.vision_patcher_max_tokens,
        )
        return await patcher.patch(
            source_code=source_code,
            critique=critique,
            spec=spec,
            render_paths=render_paths,
            facts=_facts_from_payload(geometry) if geometry else None,
        )

    @property
    def repair_generator(self):
        if self._code_generator is None:
            from app.agent.code_gen import CodeGenerator

            self._code_generator = CodeGenerator()
        return self._code_generator


def _facts_from_payload(payload: dict[str, Any] | None):
    """Rebuild worker-measured geometry facts, tolerating an absent payload."""
    if not payload:
        return None
    from app.visual_refine.facts import GeometryFacts

    return GeometryFacts.from_payload(payload)


def indeterminate_visual_report(
    *,
    issue: str,
    renders: tuple[VisualRenderEvidence, ...] = (),
    runtime_provenance: dict[str, Any] | None = None,
) -> DurableVisualReport:
    return DurableVisualReport(
        schema_version="durable-visual-report.v1",
        outcome="indeterminate",
        renders=renders,
        issues=(issue,),
        runtime_provenance=runtime_provenance,
    )
