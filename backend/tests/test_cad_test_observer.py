from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.evaluation.cad_test_runner import CadTestRunnerConfig
from app.evaluation.session import EvaluationSession
from app.evaluation.store import FileEvaluationStore


def _session(root: Path, *, source_kind: str, source_id: str) -> None:
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-1",
        source_kind=source_kind,
        source_id=source_id,
        workflow_id="workflow-1",
    )
    FileEvaluationStore(root).create(session)


@pytest.mark.asyncio
async def test_sealed_candidate_skips_non_benchmark_reports(tmp_path, monkeypatch):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _session(tmp_path, source_kind="user_task", source_id="task-1")
    called = []
    monkeypatch.setattr(
        "app.evaluation.cad_test_observer.attach_cad_test_artifacts",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-1"},
        {"candidate_revision_id": "revision-1", "artifacts": []},
        root=tmp_path,
        config=CadTestRunnerConfig(root=tmp_path),
    )

    assert called == []


@pytest.mark.asyncio
async def test_sealed_benchmark_candidate_downloads_and_attaches_real_artifacts(
    tmp_path, monkeypatch
):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _session(tmp_path, source_kind="benchmark", source_id="P01")
    payloads = []
    source = {
        "step": b"step-artifact",
        "fcstd": b"fcstd-artifact",
        "stl": b"stl-artifact",
    }

    async def download(key, destination, *, expected_size=None):
        data = source[key]
        Path(destination).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    def attach(payload, artifacts, **kwargs):
        payloads.append((payload, artifacts, kwargs))
        return "live-workflow-1"

    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_artifacts", attach)

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-1"},
        {
            "status": "succeeded",
            "candidate_revision_id": "revision-1",
            "artifacts": [
                {
                    "artifact_kind": kind,
                    "object_key": kind,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for kind, data in source.items()
            ],
        },
        root=tmp_path,
        config=CadTestRunnerConfig(root=tmp_path, scorer_image="test-image"),
    )

    assert len(payloads) == 1
    payload, artifacts, kwargs = payloads[0]
    assert payload["candidate_revision_id"] == "revision-1"
    assert set(artifacts) == {"step", "fcstd", "stl"}
    assert kwargs["case_id"] == "P01"
    assert all(path.is_file() is False for path in artifacts.values())


@pytest.mark.asyncio
async def test_sealed_candidate_integrity_mismatch_does_not_score_tampered_artifact(
    tmp_path, monkeypatch
):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _session(tmp_path, source_kind="benchmark", source_id="P01")
    called = []

    async def download(key, destination, *, expected_size=None):
        Path(destination).write_bytes(b"tampered")
        return {"sha256": "0" * 64, "size_bytes": 8}

    async def attach(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_artifacts", attach)

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-1"},
        {
            "status": "succeeded",
            "candidate_revision_id": "revision-1",
            "artifacts": [
                {
                    "artifact_kind": "step",
                    "object_key": "step",
                    "size_bytes": 8,
                    "sha256": "f" * 64,
                }
            ],
        },
        root=tmp_path,
        config=CadTestRunnerConfig(root=tmp_path),
    )

    assert called == []


@pytest.mark.asyncio
async def test_sealed_user_task_records_native_delivery_without_dataset_score(
    tmp_path, monkeypatch
):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _session(tmp_path, source_kind="user_task", source_id="task-1")
    source = {
        "step": b"step-artifact",
        "fcstd": b"fcstd-artifact",
        "stl": b"stl-artifact",
    }
    attached = []

    async def download(key, destination, *, expected_size=None):
        data = source[key]
        Path(destination).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    def native_delivery(**kwargs):
        assert set(kwargs["artifacts"]) == {"step", "fcstd", "stl"}
        return {
            "status": "passed",
            "passed": True,
            "native_solids": 1,
            "step_solids": 1,
            "stl_facets": 12,
            "stl_bounds_match": True,
        }

    def attach(payload, result, **kwargs):
        attached.append((payload, result, kwargs))

    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr(
        "app.evaluation.cad_test_observer.run_native_delivery_check", native_delivery
    )
    monkeypatch.setattr(
        "app.evaluation.cad_test_observer.attach_cad_test_observation", attach
    )

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-1"},
        {
            "status": "succeeded",
            "candidate_revision_id": "revision-1",
            "artifacts": [
                {
                    "artifact_kind": kind,
                    "object_key": kind,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
                for kind, data in source.items()
            ],
        },
        root=tmp_path,
        config=CadTestRunnerConfig(root=tmp_path, scorer_image="test-image"),
    )

    assert len(attached) == 1
    payload, result, kwargs = attached[0]
    assert payload == {
        "workflow_run_id": "workflow-1",
        "candidate_revision_id": "revision-1",
    }
    assert result["status"] == "not_applicable"
    assert result["reason"] == "no_benchmark_case"
    assert result["native_delivery"]["passed"] is True
    assert kwargs["root"] == tmp_path


@pytest.mark.asyncio
async def test_sealed_benchmark_candidate_uses_environment_configuration(tmp_path, monkeypatch):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    monkeypatch.setenv("CAD_TEST_ROOT", str(tmp_path))
    _session(tmp_path, source_kind="benchmark", source_id="P01")
    attached = []
    data = b"step-artifact"

    async def download(key, destination, *, expected_size=None):
        Path(destination).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    def attach(payload, artifacts, **kwargs):
        attached.append((payload, artifacts, kwargs))

    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_artifacts", attach)

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-1"},
        {
            "status": "succeeded",
            "candidate_revision_id": "revision-1",
            "artifacts": [
                {
                    "artifact_kind": "step",
                    "object_key": "step",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ],
        },
        root=tmp_path,
    )

    assert len(attached) == 1
    assert attached[0][2]["case_id"] == "P01"


@pytest.mark.asyncio
async def test_seal_activity_keeps_business_result_when_observer_runs(monkeypatch):
    from app.workflows.activities import McadWorkflowActivities

    expected = {"status": "succeeded", "candidate_revision_id": "revision-1"}
    observed = []

    monkeypatch.setattr(
        "app.workflows.activities.record_activity_result",
        lambda *args, **kwargs: None,
    )
    terminal = []

    def record_terminal(payload, result):
        terminal.append((payload, result))

    monkeypatch.setattr(
        "app.workflows.activities.record_workflow_terminal",
        record_terminal,
    )

    async def observer(payload, result):
        observed.append((payload, result))

    monkeypatch.setattr("app.workflows.activities.observe_sealed_candidate", observer)
    activity_instance = McadWorkflowActivities.__new__(McadWorkflowActivities)

    result = await activity_instance._run_observed(
        "agent_v2.seal_candidate",
        {"workflow_run_id": "workflow-1"},
        lambda: _return_expected(expected),
    )

    assert result is expected
    assert observed == [({"workflow_run_id": "workflow-1"}, expected)]
    assert terminal == [({"workflow_run_id": "workflow-1"}, expected)]


async def _return_expected(value):
    return value
