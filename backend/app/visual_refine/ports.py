"""Structural interfaces the refinement loop depends on.

The loop needs four capabilities: build a model from source, render it, judge
it, and patch it. Naming each as a Protocol instead of importing a concrete
class is what keeps ``loop.py`` free of Docker, Temporal, OpenAI and CadQuery --
the in-process agent and the durable workflow supply different implementations
of the same four shapes, and the hermetic tests supply fakes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from app.visual_refine.contracts import VisualCritique
from app.visual_refine.facts import GeometryFacts
from app.visual_refine.mesh_views import RenderedView, ViewSpec
from app.visual_refine.spec import DesignSpec


class BuildResult:
    """Outcome of turning source code into a model file.

    A deliberately tiny concrete class rather than a Protocol: every backend
    returns the same three facts, and callers construct it directly.
    """

    __slots__ = ("success", "model_path", "error", "detail")

    def __init__(
        self,
        *,
        success: bool,
        model_path: Path | None = None,
        error: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        if success and model_path is None:
            raise ValueError("a successful build must produce a model path")
        self.success = success
        self.model_path = model_path
        self.error = error
        self.detail = detail or {}


@runtime_checkable
class ModelBuilder(Protocol):
    """Executes candidate source and returns a mesh file to look at."""

    async def build(self, source_code: str) -> BuildResult: ...


@runtime_checkable
class ViewRenderer(Protocol):
    """Renders a model file into annotated views plus measured facts."""

    async def render(
        self,
        model_path: Path,
        output_dir: Path,
        *,
        views: Sequence[ViewSpec] | None = None,
        footer: Sequence[str] = (),
    ) -> tuple[list[RenderedView], GeometryFacts]: ...


@runtime_checkable
class VisualCritic(Protocol):
    """Judges renders against a spec. Must return indeterminate, never guess."""

    async def critique(
        self,
        *,
        spec: DesignSpec,
        render_paths: Sequence[Path],
        facts: GeometryFacts | None = None,
        source_code: str = "",
        measured_checks: Sequence[Any] = (),
    ) -> VisualCritique: ...


@runtime_checkable
class SourcePatcher(Protocol):
    """Rewrites source to clear a critique, with the renders in hand."""

    async def patch(
        self,
        *,
        source_code: str,
        critique: VisualCritique,
        spec: DesignSpec,
        render_paths: Sequence[Path] = (),
        facts: GeometryFacts | None = None,
    ) -> str: ...
