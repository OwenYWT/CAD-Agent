import pytest

from benchmark.cad_tests.mode import EvaluationMode, resolve_mode


pytestmark = pytest.mark.unit


def test_missing_mode_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("CAD_EVALUATION_MODE", raising=False)

    assert resolve_mode() is EvaluationMode.DISABLED


def test_invalid_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "enabled")

    with pytest.raises(ValueError, match="CAD_EVALUATION_MODE"):
        resolve_mode()


def test_observe_mode_is_explicit(monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")

    assert resolve_mode() is EvaluationMode.OBSERVE


def test_explicit_argument_overrides_environment(monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "experimental")

    assert resolve_mode("observe") is EvaluationMode.OBSERVE
