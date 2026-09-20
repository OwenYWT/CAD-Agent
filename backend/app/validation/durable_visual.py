"""Strict visual-gate contracts and the external vision-provider boundary."""
from __future__ import annotations

import base64
import asyncio
import json
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

    is_match: bool | None
    confidence: float = Field(ge=0, le=1)
    issues: tuple[str, ...] = ()
    suggestions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def match_has_sufficient_confidence(self):
        if self.is_match and self.confidence < 0.7:
            raise ValueError("a visual pass requires confidence >= 0.7")
        if self.is_match is None and not self.issues:
            raise ValueError("an uncertain visual judgment must explain its limitation")
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

    @model_validator(mode="after")
    def outcome_matches_judgment(self):
        if self.outcome == "indeterminate":
            if not self.issues:
                raise ValueError("indeterminate visual evidence requires an issue")
            if self.judgment is not None and (
                self.judgment.is_match is not None or len(self.renders) != 4
                or self.provider_provenance is None
            ):
                raise ValueError("indeterminate provider evidence requires an uncertain judgment and provenance")
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


_SYSTEM_PROMPT = """You are a mechanical CAD visual validation system.
Compare the four orthographic/isometric renders with the supplied design brief.
Check overall form, proportions, requested holes/features, cavities, and disconnected
or floating bodies only when their connection is part of the requested design.
Independent native components may legitimately remain disconnected.
Judge visible design agreement. Exact millimeter dimensions, named property values,
solver validity, and whether files were exported belong to kernel/artifact checks;
do not infer them from unscaled pictures or mark a visual mismatch because pictures
lack dimensions or export evidence. A visual pass does not certify those facts.
Reject observable contradictions such as four requested visible corner holes being
replaced by one central hole. Never excuse a visible missing or extra feature.
If the requested visible feature cannot be assessed because views are occluded or
insufficient, return is_match:null and explain the uncertainty in issues. Do not
turn inability to observe a feature into either a confirmed mismatch or a pass.
Ignore cosmetic styling. Return exactly one JSON object with these keys:
is_match (true, false, or null), confidence (0..1), issues (string array), and
suggestions (string array). A pass requires confidence >= 0.7. Do not use markdown."""


class DurableVisualValidator:
    """Call only the vision provider; CAD rendering stays in the sandbox."""

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

    @property
    def client(self):
        if self._client is None:
            self._client = make_llm_client()
        return self._client

    async def judge(
        self,
        *,
        objective: str,
        design_brief: dict[str, Any],
        render_paths: tuple[Path, ...],
    ) -> tuple[VisualJudgment, dict[str, Any]]:
        if len(render_paths) != 4 or any(not path.is_file() for path in render_paths):
            raise ValueError("visual judgment requires four materialized renders")
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Objective: {objective}\n"
                    "Design brief: "
                    + json.dumps(design_brief, ensure_ascii=False, sort_keys=True)
                ),
            }
        ]
        for path in render_paths:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                }
            )
        reset_chat_completion_provenance()
        response = await self.client.chat.completions.create(
            stream=True,
            model=settings.effective_vision_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        choices = list(getattr(response, "choices", ()) or ())
        if not choices:
            raise ValueError("vision provider returned no choice")
        if getattr(choices[0], "finish_reason", None) == "length":
            raise ValueError("vision provider output was truncated")
        raw = str(choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3].strip()
        parsed = json.loads(raw)
        judgment = VisualJudgment.model_validate(parsed)
        provenance = self.provenance_reader()
        if provenance is None:
            raise RuntimeError("vision judgment completed without provenance")
        return judgment, provenance

    async def report(
        self,
        *,
        objective: str,
        design_brief: dict[str, Any],
        render_paths: tuple[Path, ...],
        renders: tuple[VisualRenderEvidence, ...],
        runtime_provenance: dict[str, Any] | None,
    ) -> DurableVisualReport:
        """Return a strict tri-state report without disguising provider failure."""
        try:
            judgment, provenance = await self.judge(
                objective=objective,
                design_brief=design_brief,
                render_paths=render_paths,
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
            outcome="indeterminate" if judgment.is_match is None else "passed" if judgment.is_match else "failed",
            renders=renders,
            judgment=judgment,
            issues=judgment.issues,
            runtime_provenance=runtime_provenance,
            provider_provenance=provenance,
        )

    async def repair(
        self,
        *,
        source_code: str,
        issues: tuple[str, ...],
        suggestions: tuple[str, ...],
    ) -> tuple[str, dict[str, Any]]:
        """Apply the existing targeted visual-repair prompt with provenance."""
        reset_chat_completion_provenance()
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

    @property
    def repair_generator(self):
        if self._code_generator is None:
            from app.agent.code_gen import CodeGenerator

            self._code_generator = CodeGenerator()
        return self._code_generator


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
