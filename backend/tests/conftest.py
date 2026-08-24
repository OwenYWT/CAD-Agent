"""Shared fixtures and pytest configuration for CAD Agent tests."""
import pytest

from app.config import settings


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: pure unit tests, no external deps")
    config.addinivalue_line("markers", "docker: requires Docker daemon running")
    config.addinivalue_line("markers", "llm: requires ANTHROPIC_API_KEY")
    config.addinivalue_line("markers", "slow: tests that take >10s")
    config.addinivalue_line(
        "markers",
        "auth: exercises the login/auth system with auth_required ON",
    )
    config.addinivalue_line(
        "markers",
        "visual_refinement: exercises the VLM refinement loop instead of the "
        "injected single-shot visual gate",
    )


@pytest.fixture(autouse=True)
def _auth_off_by_default(request, monkeypatch):
    """The production default is auth_required=True (every API route needs a login
    token). The bulk of the suite predates auth and exercises non-auth features with
    `api_keys=[]` meaning "auth off". To keep that contract explicit, default every
    test to auth_required=False here. Tests that specifically verify auth behavior
    opt back in with the `auth` marker (or set auth_required True themselves)."""
    if request.node.get_closest_marker("auth"):
        return
    monkeypatch.setattr(settings, "auth_required", False, raising=False)


@pytest.fixture(autouse=True)
def _visual_refinement_off_by_default(request, monkeypatch):
    """Keep the hermetic suite off the VLM refinement path by default.

    Settings load from `backend/.env`, and every developer is told to create one
    with real provider credentials. Without this, whether a test calls a live
    vision API depends on whether the machine running it happens to have a key --
    the suite would pass on CI and make paid network calls on a laptop.

    Tests that drive the visual gate inject their own renderer and validator into
    the orchestrator, which is the single-shot path; the loop's own behaviour is
    covered hermetically in test_visual_refine_loop.py with fake ports. A test
    that genuinely wants the loop opts in with the `visual_refinement` marker.
    """
    if request.node.get_closest_marker("visual_refinement"):
        return
    monkeypatch.setattr(settings, "visual_refinement_enabled", False, raising=False)
