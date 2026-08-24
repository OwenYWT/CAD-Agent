"""The refinement loop: build -> measure -> render -> critique -> patch -> repeat.

Two properties matter more than the iteration count.

**Monotonicity.** The old repair path executed a fix and kept it if it merely
ran. A fix that ran but deleted the feature it was asked to attach still won.
Here every candidate is scored, the best-scoring one is what comes out, and a
patch that fails to build or scores worse is discarded. Refinement can then only
help: worst case it returns the input unchanged.

**Honesty.** A provider outage produces an indeterminate outcome that keeps the
original source, never a silent pass. The loop distinguishes "proved good",
"improved but not proved", "could not judge" and "could not build", and callers
gate on the difference.

The loop owns no IO of its own -- builder, renderer, critic and patcher all
arrive as ports -- so the same code runs behind the in-process agent and behind
the durable workflow.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.visual_refine.contracts import (
    RefinementAttempt,
    RefinementOutcome,
    VisualCritique,
)
from app.visual_refine.facts import GeometryFacts
from app.visual_refine.mesh_views import STANDARD_VIEWS, SECTION_VIEW, ViewSpec
from app.visual_refine.ports import ModelBuilder, SourcePatcher, ViewRenderer, VisualCritic
from app.visual_refine.spec import DesignSpec

logger = logging.getLogger(__name__)

# Score at or above which a candidate is good enough to stop early even though
# some requirement came back "not_visible". Below it, spending another
# iteration is usually worth the tokens.
_GOOD_ENOUGH_SCORE = 0.995


@dataclass
class _Candidate:
    source_code: str
    score: float
    critique: VisualCritique | None
    facts: GeometryFacts | None
    render_paths: tuple[Path, ...]
    verdict: str

    @property
    def passed(self) -> bool:
        return self.verdict == "passed"


class VisualRefinementLoop:
    """Drive candidate models toward the request using rendered visual feedback."""

    def __init__(
        self,
        *,
        builder: ModelBuilder,
        renderer: ViewRenderer,
        critic: VisualCritic,
        patcher: SourcePatcher,
        max_iterations: int = 3,
        views: Sequence[ViewSpec] = STANDARD_VIEWS + (SECTION_VIEW,),
        on_progress=None,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self._builder = builder
        self._renderer = renderer
        self._critic = critic
        self._patcher = patcher
        self._max_iterations = max_iterations
        self._views = tuple(views)
        self._on_progress = on_progress

    async def run(
        self,
        *,
        source_code: str,
        spec: DesignSpec,
        work_dir: Path,
        model_path: Path | None = None,
    ) -> RefinementOutcome:
        """Refine ``source_code`` until it satisfies ``spec`` or the budget ends.

        ``model_path`` lets a caller hand in a model it has already built, so
        the first iteration costs no extra execution.
        """
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        attempts: list[RefinementAttempt] = []
        best: _Candidate | None = None
        current_source = source_code
        prebuilt = model_path

        for index in range(self._max_iterations):
            iteration_dir = work_dir / f"iteration-{index:02d}"
            if iteration_dir.exists():
                shutil.rmtree(iteration_dir, ignore_errors=True)
            iteration_dir.mkdir(parents=True, exist_ok=True)

            candidate, attempt = await self._evaluate(
                source_code=current_source,
                spec=spec,
                iteration_dir=iteration_dir,
                index=index,
                prebuilt=prebuilt,
            )
            prebuilt = None
            attempts.append(attempt)

            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate

            if candidate is not None and candidate.passed:
                if candidate.score >= _GOOD_ENOUGH_SCORE:
                    break
                # Passed the gate but left something unverified. One more turn is
                # only worth taking if there is budget and something to act on.
                if index >= self._max_iterations - 1 or not candidate.critique:
                    break
                if not candidate.critique.failures:
                    break

            if index >= self._max_iterations - 1:
                break

            patch_source = self._select_patch_source(candidate, best, current_source)
            critique = self._select_patch_critique(candidate, best)
            if critique is None or not critique.failures:
                # Nothing actionable: either the build failed with no critique or
                # the inspector could not judge. Another blind rewrite would be a
                # coin flip, so stop and return the best proven candidate.
                break

            await self._progress(
                f"visual refinement {index + 1}/{self._max_iterations}: "
                + "; ".join(check.requirement for check in critique.failures[:2])
            )
            try:
                current_source = await self._patcher.patch(
                    source_code=patch_source,
                    critique=critique,
                    spec=spec,
                    render_paths=(
                        candidate.render_paths if candidate is not None else ()
                    ),
                    facts=candidate.facts if candidate is not None else None,
                )
            except Exception as exc:
                logger.warning("visual patch step failed: %s: %s", type(exc).__name__, exc)
                break

        return self._finalize(source_code, best, tuple(attempts))

    async def _evaluate(
        self,
        *,
        source_code: str,
        spec: DesignSpec,
        iteration_dir: Path,
        index: int,
        prebuilt: Path | None,
    ) -> tuple[_Candidate | None, RefinementAttempt]:
        """Build, render, measure and judge one candidate."""
        digest = hashlib.sha256(source_code.encode("utf-8")).hexdigest()

        model_path = prebuilt
        if model_path is None:
            build = await self._builder.build(source_code)
            if not build.success or build.model_path is None:
                return None, RefinementAttempt(
                    index=index,
                    source_sha256=digest,
                    score=0.0,
                    verdict="build_failed",
                    error=build.error[:2000],
                )
            model_path = build.model_path

        try:
            views, facts = await self._renderer.render(
                Path(model_path),
                iteration_dir,
                views=self._views,
                footer=(f"request: {spec.objective[:110]}",),
            )
        except Exception as exc:
            logger.warning("render step failed: %s: %s", type(exc).__name__, exc)
            return None, RefinementAttempt(
                index=index,
                source_sha256=digest,
                score=0.0,
                verdict="indeterminate",
                error=f"render_failed:{type(exc).__name__}",
            )

        measured = spec.measured_checks(facts)
        render_paths = tuple(view.path for view in views)
        critique = await self._critic.critique(
            spec=spec,
            render_paths=render_paths,
            facts=facts,
            source_code=source_code,
            measured_checks=measured,
        )

        match = critique.is_match
        verdict = "passed" if match else ("indeterminate" if match is None else "failed")
        score = critique.score()
        candidate = _Candidate(
            source_code=source_code,
            score=score,
            critique=critique,
            facts=facts,
            render_paths=render_paths,
            verdict=verdict,
        )
        return candidate, RefinementAttempt(
            index=index,
            source_sha256=digest,
            score=score,
            verdict=verdict,  # type: ignore[arg-type]
            critique=critique,
            geometry=facts.as_dict(),
            render_files=tuple(path.name for path in render_paths),
        )

    @staticmethod
    def _select_patch_source(
        candidate: _Candidate | None,
        best: _Candidate | None,
        current_source: str,
    ) -> str:
        """Patch the candidate that actually built, not a dead end.

        When an iteration fails to build, its source is a worse starting point
        than the best solid produced so far, so the loop backtracks rather than
        stacking a second speculative edit on a broken file.
        """
        if candidate is not None:
            return candidate.source_code
        if best is not None:
            return best.source_code
        return current_source

    @staticmethod
    def _select_patch_critique(
        candidate: _Candidate | None, best: _Candidate | None
    ) -> VisualCritique | None:
        if candidate is not None and candidate.critique is not None:
            return candidate.critique
        if best is not None:
            return best.critique
        return None

    def _finalize(
        self,
        original_source: str,
        best: _Candidate | None,
        attempts: tuple[RefinementAttempt, ...],
    ) -> RefinementOutcome:
        if best is None:
            # Nothing ever built and rendered. Hand the input straight back: the
            # loop has no evidence to justify replacing it.
            failed_build = any(item.verdict == "build_failed" for item in attempts)
            return RefinementOutcome(
                status="failed" if failed_build else "indeterminate",
                source_code=original_source,
                attempts=attempts,
                changed=False,
            )

        changed = best.source_code.strip() != original_source.strip()
        if best.passed:
            status = "passed"
        elif best.critique is not None and best.critique.is_match is None:
            status = "indeterminate"
        elif changed:
            status = "improved"
        else:
            status = "unchanged"

        return RefinementOutcome(
            status=status,
            source_code=best.source_code,
            critique=best.critique,
            geometry=best.facts.as_dict() if best.facts else {},
            attempts=attempts,
            render_paths=tuple(str(path) for path in best.render_paths),
            changed=changed,
        )

    async def _progress(self, message: str) -> None:
        if self._on_progress is None:
            return
        import asyncio

        result = self._on_progress(message)
        if asyncio.iscoroutine(result):
            await result
