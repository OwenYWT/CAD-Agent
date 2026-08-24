"""Legacy renderer surface, now backed by the depth-buffered engine.

``CADRenderer.render_stl`` keeps its signature because the orchestrator, the
multi-step path, the eval harness and several tests call it. What changed is
underneath: renders come from :mod:`app.visual_refine.mesh_views`, so they are
opaque, framed on the subject and carry dimensions and a millimetre grid,
instead of the semi-transparent unscaled matplotlib output that made through
holes look like solid pillars.

New code should prefer ``app.visual_refine.renderers.MeshViewRenderer``, which
also returns measured geometry alongside the images.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.visual_refine.mesh_views import (
    SECTION_VIEW,
    STANDARD_VIEWS,
    ViewSpec,
    render_views,
)

logger = logging.getLogger(__name__)


@dataclass
class CameraAngle:
    """Kept for callers that build their own view list."""

    name: str
    elevation: float  # degrees
    azimuth: float  # degrees
    distance: float = 0  # unused; the engine frames each view on its extents

    def to_view(self) -> ViewSpec:
        for view in STANDARD_VIEWS + (SECTION_VIEW,):
            if view.name == self.name:
                return view
        return ViewSpec(
            name=self.name, elevation=self.elevation, azimuth=self.azimuth
        )


STANDARD_ANGLES = [
    CameraAngle(view.name, elevation=view.elevation, azimuth=view.azimuth)
    for view in STANDARD_VIEWS
]


class CADRenderer:
    """Renders a model file to PNG views on disk."""

    def __init__(self, *, width: int = 768, height: int = 768) -> None:
        self._width = width
        self._height = height

    def render_stl(
        self,
        stl_path: Path,
        output_dir: Path,
        angles: list[CameraAngle] | None = None,
        *,
        include_section: bool = False,
        footer: tuple[str, ...] = (),
    ) -> list[Path]:
        """Render ``stl_path`` and return the written image paths.

        Produces the four standard views unless ``include_section`` asks for the
        cutaway as well; callers of this legacy surface expect exactly four
        files. The refinement loop uses ``MeshViewRenderer`` and opts into the
        section view there.

        Returns an empty list rather than raising when the mesh cannot be
        rendered: callers treat "no renders" as an honest indeterminate visual
        result, and a render failure must not take down a generation that
        otherwise succeeded.
        """
        selected = [angle.to_view() for angle in (angles or STANDARD_ANGLES)]
        if include_section:
            selected.append(SECTION_VIEW)
        try:
            rendered = render_views(
                Path(stl_path),
                Path(output_dir),
                views=selected,
                width=self._width,
                height=self._height,
                footer=footer,
            )
        except Exception as exc:
            logger.warning("CAD render failed for %s: %s", stl_path, exc)
            return []
        return [view.path for view in rendered]
