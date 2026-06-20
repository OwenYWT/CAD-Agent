"""Shared fixtures and pytest configuration for CAD Agent tests."""
import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: pure unit tests, no external deps")
    config.addinivalue_line("markers", "docker: requires Docker daemon running")
    config.addinivalue_line("markers", "llm: requires ANTHROPIC_API_KEY")
    config.addinivalue_line("markers", "slow: tests that take >10s")
