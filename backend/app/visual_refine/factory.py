"""The one module in this package that reads application settings.

Everything else takes its collaborators as arguments. Concentrating the
configuration read here means the loop, critic, patcher, spec and renderers stay
importable (and testable) without a configured provider, and a deployment that
has no vision credentials fails at one obvious place with one obvious message
instead of somewhere deep in a workflow.
"""
from __future__ import annotations

from typing import Any

from app.config import make_llm_client, settings
from app.visual_refine.critic import VisionCritic
from app.visual_refine.patcher import VisionPatcher
from app.visual_refine.renderers import MeshViewRenderer


class VisionUnavailable(RuntimeError):
    """Raised when visual refinement is requested without usable credentials."""


def vision_ready(app_settings: Any = settings) -> bool:
    """True when a vision-capable model is configured and reachable in principle."""
    return bool(
        app_settings.visual_refinement_enabled
        and app_settings.has_llm_credentials
        and app_settings.effective_vision_model.strip()
    )


def vision_unavailable_reason(app_settings: Any = settings) -> str:
    if not app_settings.visual_refinement_enabled:
        return "visual refinement is disabled (VISUAL_REFINEMENT_ENABLED=false)"
    if not app_settings.has_llm_credentials:
        return app_settings.llm_credentials_error
    if not app_settings.effective_vision_model.strip():
        return "VISION_MODEL is not configured"
    return ""


def make_vision_critic(
    *, client: Any = None, app_settings: Any = settings
) -> VisionCritic:
    """Build the default critic. Raises rather than degrading to a fake."""
    reason = vision_unavailable_reason(app_settings)
    if reason:
        raise VisionUnavailable(reason)
    from app.llm import get_last_chat_completion_provenance

    return VisionCritic(
        client=client if client is not None else make_llm_client(),
        model=app_settings.effective_vision_model,
        max_tokens=app_settings.vision_critic_max_tokens,
        provenance_reader=get_last_chat_completion_provenance,
    )


def make_vision_patcher(
    *, client: Any = None, app_settings: Any = settings
) -> VisionPatcher:
    """Build the default multimodal repair generator."""
    reason = vision_unavailable_reason(app_settings)
    if reason:
        raise VisionUnavailable(reason)
    from app.llm import get_last_chat_completion_provenance

    return VisionPatcher(
        client=client if client is not None else make_llm_client(),
        # Repair reads images and writes code, so it runs on the vision model
        # too; a text-only coding model cannot see the defect it is fixing.
        model=app_settings.effective_vision_model,
        max_tokens=app_settings.vision_patcher_max_tokens,
        provenance_reader=get_last_chat_completion_provenance,
    )


def make_view_renderer(*, app_settings: Any = settings) -> MeshViewRenderer:
    """Build the default renderer at the configured resolution."""
    return MeshViewRenderer(
        width=app_settings.vision_render_px,
        height=app_settings.vision_render_px,
        supersample=app_settings.vision_render_supersample,
    )
