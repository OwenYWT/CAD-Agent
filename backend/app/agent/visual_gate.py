"""In-process adapter between the agent and the VLM refinement loop.

The state machine and the multi-step builder both used to carry their own copy
of "render, ask the vision model, maybe repair, maybe re-execute", with
different retry budgets and different definitions of success. Both now call
:func:`refine_visually` instead, so there is one visual gate with one policy and
one place to change it.

Everything provider-specific and CAD-specific stays behind ports: this module
only adapts the agent's sandbox executor to :class:`ModelBuilder` and hands the
loop its collaborators.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from app.config import settings
from app.visual_refine.contracts import RefinementOutcome
from app.visual_refine.factory import (
    VisionUnavailable,
    make_view_renderer,
    make_vision_critic,
    make_vision_patcher,
    vision_ready,
    vision_unavailable_reason,
)
from app.visual_refine.loop import VisualRefinementLoop
from app.visual_refine.ports import BuildResult
from app.visual_refine.spec import DesignSpec, build_spec

logger = logging.getLogger(__name__)

_MODEL_SUFFIXES = (".stl", ".step", ".stp")


class SandboxModelBuilder:
    """Adapts the agent's CadQuery executor to the loop's ``ModelBuilder`` port.

    Builds are indexed by source digest rather than by order, because the loop
    may pick an earlier candidate over the last one. The caller therefore needs
    the execution result *for the source that won*, and every other work tree
    can be deleted; keying on "the last build" would occasionally throw away the
    winner's artifacts and keep a regression's.
    """

    def __init__(self, executor: Any) -> None:
        self._executor = executor
        self._builds: dict[str, tuple[Path | None, Any]] = {}

    @staticmethod
    def _digest(source_code: str) -> str:
        return hashlib.sha256(source_code.encode("utf-8")).hexdigest()

    async def build(self, source_code: str) -> BuildResult:
        result = await self._executor.execute(source_code)
        raw_work_dir = getattr(result, "work_dir", None)
        work_dir = Path(raw_work_dir) if raw_work_dir is not None else None
        self._builds[self._digest(source_code)] = (work_dir, result)

        if not result.success:
            return BuildResult(
                success=False,
                error=str(getattr(result, "error_message", "") or "execution failed"),
                detail={
                    "error_type": getattr(result, "error_type", None),
                    "traceback": getattr(result, "traceback", ""),
                },
            )
        model = find_model_file(work_dir)
        if model is None:
            return BuildResult(
                success=False,
                error="the build produced no STL or STEP output to inspect",
            )
        return BuildResult(success=True, model_path=model, detail={"result": result})

    def result_for(self, source_code: str) -> Any | None:
        """The sandbox result for a source the loop actually built, if any."""
        entry = self._builds.get(self._digest(source_code))
        return entry[1] if entry else None

    def work_dir_for(self, source_code: str) -> Path | None:
        entry = self._builds.get(self._digest(source_code))
        return entry[0] if entry else None

    def cleanup(self, *, keep_source: str | None = None) -> None:
        """Delete every work tree except the one backing ``keep_source``."""
        keep = self._digest(keep_source) if keep_source is not None else None
        for digest, (work_dir, _) in list(self._builds.items()):
            if digest == keep or work_dir is None:
                continue
            shutil.rmtree(work_dir, ignore_errors=True)
            self._builds.pop(digest, None)


def find_model_file(work_dir: Path | None) -> Path | None:
    """Locate the inspectable model a build produced, preferring mesh output."""
    if work_dir is None:
        return None
    root = Path(work_dir)
    for suffix in _MODEL_SUFFIXES:
        matches = sorted(root.rglob(f"*{suffix}"))
        if matches:
            return matches[0]
    return None


def spec_for(
    *,
    objective: str,
    plan: Any | None = None,
    expected_hole_count: int | None = None,
) -> DesignSpec:
    """Build the requirement set for a generation request."""
    return build_spec(
        objective=objective,
        plan=plan,
        expected_hole_count=expected_hole_count,
    )


async def refine_visually(
    *,
    source_code: str,
    objective: str,
    executor: Any,
    work_dir: Path,
    plan: Any | None = None,
    model_path: Path | None = None,
    max_iterations: int | None = None,
    on_progress: Callable[[str], Any] | None = None,
) -> tuple[RefinementOutcome, SandboxModelBuilder] | None:
    """Run the visual refinement loop for an in-process generation.

    Returns the outcome together with the builder, so the caller can pick up the
    sandbox result and work directory for whichever candidate won without
    re-executing it.

    Returns ``None`` when visual refinement is not configured -- the caller then
    proceeds exactly as it would have without a visual gate, rather than being
    blocked or silently told the model passed.
    """
    if not vision_ready(settings):
        logger.info("visual refinement skipped: %s", vision_unavailable_reason(settings))
        return None

    try:
        critic = make_vision_critic()
        patcher = make_vision_patcher()
    except VisionUnavailable as exc:
        logger.info("visual refinement skipped: %s", exc)
        return None

    builder = SandboxModelBuilder(executor)
    loop = VisualRefinementLoop(
        builder=builder,
        renderer=make_view_renderer(),
        critic=critic,
        patcher=patcher,
        max_iterations=(
            max_iterations
            if max_iterations is not None
            else settings.visual_refinement_max_iterations
        ),
        on_progress=on_progress,
    )
    outcome = await loop.run(
        source_code=source_code,
        spec=spec_for(objective=objective, plan=plan),
        work_dir=Path(work_dir),
        model_path=model_path,
    )
    # Keep only the work tree behind the candidate that won; the rest is scratch.
    builder.cleanup(keep_source=outcome.source_code)
    return outcome, builder
