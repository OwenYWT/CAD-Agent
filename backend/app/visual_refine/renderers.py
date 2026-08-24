"""Default ``ViewRenderer``: render annotated views and measure the mesh.

Rendering and measuring are CPU-bound and synchronous, so they run on a worker
thread rather than blocking the event loop that is also streaming progress to
the browser.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Sequence

from app.visual_refine.facts import GeometryFacts, measure
from app.visual_refine.mesh_views import (
    SECTION_VIEW,
    STANDARD_VIEWS,
    RenderedView,
    ViewSpec,
    render_views,
)

DEFAULT_VIEWS: tuple[ViewSpec, ...] = STANDARD_VIEWS + (SECTION_VIEW,)


class MeshViewRenderer:
    """Turn a model file into annotated renders plus measured geometry facts."""

    def __init__(
        self,
        *,
        width: int = 768,
        height: int = 768,
        supersample: int = 2,
        views: Sequence[ViewSpec] = DEFAULT_VIEWS,
        detect_holes: bool = True,
    ) -> None:
        self._width = width
        self._height = height
        self._supersample = supersample
        self._views = tuple(views)
        self._detect_holes = detect_holes

    async def render(
        self,
        model_path: Path,
        output_dir: Path,
        *,
        views: Sequence[ViewSpec] | None = None,
        footer: Sequence[str] = (),
    ) -> tuple[list[RenderedView], GeometryFacts]:
        selected = tuple(views) if views is not None else self._views
        return await asyncio.to_thread(
            self._render_sync,
            Path(model_path),
            Path(output_dir),
            selected,
            tuple(footer),
        )

    def _render_sync(
        self,
        model_path: Path,
        output_dir: Path,
        views: tuple[ViewSpec, ...],
        footer: tuple[str, ...],
    ) -> tuple[list[RenderedView], GeometryFacts]:
        facts = measure(model_path, detect_holes=self._detect_holes)
        rendered = render_views(
            model_path,
            output_dir,
            views=views,
            width=self._width,
            height=self._height,
            supersample=self._supersample,
            footer=footer,
        )
        return rendered, facts
