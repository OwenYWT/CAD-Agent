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
