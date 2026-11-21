from __future__ import annotations

import pytest


def test_missing_mode_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("CAD_EVALUATION_MODE", raising=False)

    from app.evaluation.mode import EvaluationMode, resolve_mode

    assert resolve_mode() is EvaluationMode.DISABLED


def test_invalid_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "enabled")

    from app.evaluation.mode import resolve_mode

    with pytest.raises(ValueError, match="CAD_EVALUATION_MODE"):
        resolve_mode()


@pytest.mark.parametrize("value", ["disabled", "observe", "experimental"])
def test_explicit_modes_are_resolved(value):
    from app.evaluation.mode import EvaluationMode, resolve_mode

    assert resolve_mode(value) is EvaluationMode(value)


def test_request_payload_cannot_override_process_mode(monkeypatch):
    monkeypatch.setenv("CAD_EVALUATION_MODE", "disabled")

    from app.evaluation.mode import EvaluationMode, resolve_mode

    assert resolve_mode() is EvaluationMode.DISABLED
    assert resolve_mode(None) is EvaluationMode.DISABLED


def test_api_and_worker_parsers_share_mode_vocabulary():
    from app.run import build_parser as build_api_parser
    from app.workers.run import build_parser as build_worker_parser

    api_modes = build_api_parser()._option_string_actions["--evaluation-mode"].choices
    worker_modes = build_worker_parser()._option_string_actions["--evaluation-mode"].choices

    assert api_modes == worker_modes == ["disabled", "observe", "experimental"]


def test_api_parser_defaults_to_disabled():
    from app.run import build_parser

    args = build_parser().parse_args([])

    assert args.evaluation_mode == "disabled"


def test_worker_parser_preserves_worker_configuration_options():
    from app.workers.run import build_parser

    args = build_parser().parse_args(
        ["--evaluation-mode", "observe", "--namespace", "test", "--task-queue", "queue"]
    )

    assert args.evaluation_mode == "observe"
    assert args.namespace == "test"
    assert args.task_queue == "queue"
