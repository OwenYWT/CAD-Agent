"""Deterministic four-view STL rendering for the isolated MCAD worker.

The rendering itself lives in ``mesh_views``, which is copied into the image
next to this entry point and is byte-identical to the control plane's copy
(``backend/app/visual_refine/mesh_views.py``). Sharing one implementation is
what lets the durable gate hash a render here and have the same mesh produce the
same hash anywhere else; two lookalike renderers would quietly diverge and
invalidate stored evidence.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/.cache")

# Inside the worker the module sits beside this file; in the control plane and
# in tests it is imported from the application package. Same file either way.
try:  # pragma: no cover - exercised by whichever side is running
    from app.visual_refine.facts import measure
    from app.visual_refine.mesh_views import (
        SECTION_VIEW,
        STANDARD_VIEWS,
        render_views,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from facts import measure  # type: ignore
    from mesh_views import SECTION_VIEW, STANDARD_VIEWS, render_views  # type: ignore


def render_four_views(
    source: Path,
    output_root: Path,
    *,
    width: int = 768,
    height: int = 768,
    include_section: bool = True,
    objective: str = "",
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Render the durable view set and return paths plus hashable evidence.

    The four standard views are a durable contract and are always produced in
    order. The section view is additional evidence for cavities and blind holes;
    it is reported separately so stored four-render evidence stays valid.

    Measured geometry travels with the renders because measuring belongs on the
    same side of the isolation boundary as the mesh: the control plane never has
    to open the model itself, and the vision model downstream gets numbers it
    cannot talk itself out of.
    """
    views = list(STANDARD_VIEWS)
    if include_section:
        views.append(SECTION_VIEW)

    footer = (f"request: {objective[:110]}",) if objective.strip() else ()
    rendered = render_views(
        Path(source),
        Path(output_root),
        views=views,
        width=width,
        height=height,
        footer=footer,
    )

    outputs: dict[str, Path] = {}
    standard_evidence: list[dict[str, Any]] = []
    extra_evidence: list[dict[str, Any]] = []
    for view in rendered:
        outputs[view.name] = view.path
        record = view.evidence()
        if view.is_section:
            extra_evidence.append(record)
        else:
            standard_evidence.append(record)

    try:
        geometry = measure(Path(source)).as_dict()
    except Exception as exc:  # measurement never blocks a successful render
        geometry = {"error": f"{type(exc).__name__}: {exc}"[:400]}

    return outputs, {
        "schema_version": "durable-render-report.v1",
        "views": standard_evidence,
        "supplementary_views": extra_evidence,
        "geometry": geometry,
    }
