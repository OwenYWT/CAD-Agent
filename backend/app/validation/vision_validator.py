"""Single-shot visual judgement, backed by the shared VLM critic.

This is the degraded mode of the visual gate: one look, one verdict, no
iteration. The orchestrator holds it, the state machine falls back to it when
refinement is not configured, and the hermetic end-to-end harness injects a fake
in its place.

The judging itself now goes through :mod:`app.visual_refine`, so a single-shot
verdict uses the same requirement set, the same measured-versus-observed split
and the same derived verdict as the full loop. What stays here is the small
legacy result shape callers already handle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app.config import make_llm_client, settings

logger = logging.getLogger(__name__)


@dataclass
class VisionValidationResult:
    # True = matches description, False = mismatch (triggers a fix retry),
    # None = INDETERMINATE (could not evaluate — no renders / parse failure).
    # Indeterminate must never read as a pass: it does not flip success, does not
    # trigger a fix, and is surfaced honestly in the inspect report.
    is_match: bool | None
    confidence: float
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class VisionValidator:
    def __init__(self, *, client: Any = None) -> None:
        self._client = client

    @property
    def client(self):
        if self._client is None:
            if not settings.has_llm_credentials:
                raise RuntimeError(settings.llm_credentials_error)
            self._client = make_llm_client()
        return self._client

    async def validate(
        self,
        user_description: str,
        render_paths: Sequence[Path],
        code: str,
        *,
        model_path: Path | None = None,
        plan: Any = None,
    ) -> VisionValidationResult:
        """Judge renders against a description in one call.

        ``model_path`` is optional but worth passing: with the mesh in hand the
        gate settles size, hole count and body count by measuring instead of
        asking a model to read them off a picture.
        """
        usable = [Path(path) for path in render_paths if Path(path).is_file()]
        if not usable:
            return VisionValidationResult(
                is_match=None,
                confidence=0.0,
                issues=["无渲染图可用，视觉校验未执行"],
                suggestions=[],
            )

        from app.visual_refine.critic import VisionCritic
        from app.visual_refine.spec import build_spec

        spec = build_spec(objective=user_description, plan=plan)
        facts = None
        measured: tuple[Any, ...] = ()
        if model_path is not None:
            try:
                from app.visual_refine.facts import measure

                facts = measure(model_path)
                measured = spec.measured_checks(facts)
            except Exception as exc:  # measurement is an enhancement, not a gate
                logger.warning("geometry measurement skipped: %s", exc)

        try:
            critic = VisionCritic(
                client=self.client,
                model=settings.effective_vision_model,
                max_tokens=settings.vision_critic_max_tokens,
            )
        except Exception as exc:
            logger.warning("Vision validation unavailable: %s", exc)
            return VisionValidationResult(
                is_match=None,
                confidence=0.0,
                issues=["视觉校验不可用，结果不可信"],
                suggestions=[],
            )

        critique = await critic.critique(
            spec=spec,
            render_paths=usable,
            facts=facts,
            source_code=code,
            measured_checks=measured,
        )
        if critique.indeterminate:
            logger.warning(
                "Vision validation indeterminate: %s", critique.indeterminate_reason
            )
            return VisionValidationResult(
                is_match=None,
                confidence=0.0,
                issues=["视觉校验解析失败，结果不可信"],
                suggestions=[],
            )

        return VisionValidationResult(
            is_match=critique.is_match,
            confidence=critique.confidence,
            issues=[
                check.observed or check.requirement for check in critique.failures
            ]
            + list(critique.issues),
            suggestions=list(critique.suggestions),
        )
