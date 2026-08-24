"""VLM-in-the-loop refinement for generated CAD.

The package renders a generated solid the way an engineer would look at it --
depth-buffered orthographic views carrying real dimensions, a millimetre grid
and a section cut -- measures what can be measured, asks a vision model only
about what genuinely needs looking at, and feeds those same renders back into
the repair step so the model fixes the code while seeing what the code built.

Layering, outermost last:

    mesh_views / facts     pure geometry and pixels (numpy, trimesh, Pillow)
    contracts / spec       pure data: requirements, checks, critiques, outcomes
    prompts                prompt text, versioned on its own
    ports                  the four interfaces the loop depends on
    critic / patcher       the model-provider boundary
    renderers              the default ViewRenderer
    loop                   orchestration, provider-agnostic
    factory                the only reader of application settings

Import the leaf you need. Callers that just want the default wiring should use
``factory`` plus ``VisualRefinementLoop``.
"""
from __future__ import annotations

from app.visual_refine.contracts import (
    RefinementAttempt,
    RefinementOutcome,
    RequirementCheck,
    VisualCritique,
)
from app.visual_refine.facts import GeometryFacts, HoleFact, measure
from app.visual_refine.loop import VisualRefinementLoop
from app.visual_refine.mesh_views import (
    ALL_VIEWS,
    SECTION_VIEW,
    STANDARD_VIEWS,
    RenderedView,
    ViewSpec,
    render_views,
)
from app.visual_refine.ports import (
    BuildResult,
    ModelBuilder,
    SourcePatcher,
    ViewRenderer,
    VisualCritic,
)
from app.visual_refine.spec import DesignSpec, Requirement, build_spec

__all__ = [
    "ALL_VIEWS",
    "BuildResult",
    "DesignSpec",
    "GeometryFacts",
    "HoleFact",
    "ModelBuilder",
    "RefinementAttempt",
    "RefinementOutcome",
    "RenderedView",
    "Requirement",
    "RequirementCheck",
    "SECTION_VIEW",
    "STANDARD_VIEWS",
    "SourcePatcher",
    "ViewRenderer",
    "ViewSpec",
    "VisualCritic",
    "VisualCritique",
    "VisualRefinementLoop",
    "build_spec",
    "measure",
    "render_views",
]
