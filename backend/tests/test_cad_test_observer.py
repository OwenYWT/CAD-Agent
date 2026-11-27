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
    seal = FileEvaluationStore(tmp_path).cad_test_seal("live-workflow-1")
    assert seal["candidate_revision_id"] == "revision-1"
    assert seal["artifacts"] == {
        kind: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        for kind, data in source.items()
    }


@pytest.mark.asyncio
async def test_sealed_2d_benchmark_candidate_is_not_discarded(tmp_path, monkeypatch):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-2d",
        source_kind="benchmark",
        source_id="X01",
        source_metadata={"modeling_path": "2d"},
        workflow_id="workflow-2d",
    )
    FileEvaluationStore(tmp_path).create(session)
    data = b"dxf-artifact"
    attached = []

    async def download(_key, destination, *, expected_size=None):
        Path(destination).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    def attach(payload, artifacts, **kwargs):
        attached.append(
            (
                payload,
                {kind: path.read_bytes() for kind, path in artifacts.items()},
                kwargs,
            )
        )

    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_artifacts", attach)

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-2d"},
        {
            "status": "succeeded",
            "candidate_revision_id": "revision-2d",
            "artifacts": [
                {
                    "artifact_kind": "dxf",
                    "object_key": "dxf",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "is_primary": True,
                }
            ],
        },
        root=tmp_path,
        config=CadTestRunnerConfig(root=tmp_path),
    )

    assert len(attached) == 1
    assert set(attached[0][1]) == {"dxf"}
    assert attached[0][2]["modeling_path"] == "2d"


@pytest.mark.asyncio
async def test_sealed_assembly_uses_explicit_primary_artifact_bundle(tmp_path, monkeypatch):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-assembly",
        source_kind="benchmark",
        source_id="A01",
        source_metadata={"modeling_path": "assembly"},
        workflow_id="workflow-assembly",
    )
    FileEvaluationStore(tmp_path).create(session)
    attached = []

    async def download(key, destination, *, expected_size=None):
        data = key.encode("ascii")
        Path(destination).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    def attach(payload, artifacts, **kwargs):
        attached.append(
            (
                payload,
                {kind: path.read_bytes() for kind, path in artifacts.items()},
                kwargs,
            )
        )

    artifacts = []
    for role, primary in (("part", False), ("assembly", True)):
        for kind in ("step", "fcstd", "stl"):
            data = f"{role}-{kind}".encode("ascii")
            artifacts.append(
                {
                    "artifact_kind": kind,
                    "object_key": f"{role}-{kind}",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "is_primary": primary,
                }
            )
    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_artifacts", attach)

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-assembly"},
        {
            "status": "succeeded",
            "candidate_revision_id": "revision-assembly",
            "artifacts": artifacts,
        },
        root=tmp_path,
        config=CadTestRunnerConfig(root=tmp_path),
    )

    assert len(attached) == 1
    assert set(attached[0][1]) == {"step", "fcstd", "stl"}
    assert all(b"assembly" in value for value in attached[0][1].values())
    assert attached[0][2]["modeling_path"] == "assembly"


@pytest.mark.asyncio
async def test_sealed_assembly_rejects_combine_prefixed_artifacts_without_metadata(
    tmp_path, monkeypatch
):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    session = EvaluationSession.create(
        mode="observe",
        evaluation_run_id="live-workflow-assembly-prefix",
        source_kind="benchmark",
        source_id="A02",
        source_metadata={"modeling_path": "assembly"},
        workflow_id="workflow-assembly-prefix",
    )
    FileEvaluationStore(tmp_path).create(session)
    attached = []

    async def download(key, destination, *, expected_size=None):
        data = key.encode("ascii")
        Path(destination).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    def attach(payload, artifacts, **kwargs):
        attached.append({kind: path.read_bytes() for kind, path in artifacts.items()})

    artifact_rows = []
    for prefix in ("part-01", "combine-final"):
        for kind in ("step", "fcstd", "stl"):
            data = f"{prefix}-{kind}".encode("ascii")
            artifact_rows.append(
                {
                    "artifact_kind": kind,
                    "filename": f"{prefix}-model.{kind}",
                    "object_key": f"{prefix}-{kind}",
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_artifacts", attach)

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-assembly-prefix"},
        {
            "status": "succeeded",
            "candidate_revision_id": "revision-assembly-prefix",
            "artifacts": artifact_rows,
        },
        root=tmp_path,
        config=CadTestRunnerConfig(root=tmp_path),
    )

    assert not attached
    report = json.loads(FileEvaluationStore(tmp_path).path_for(session.evaluation_run_id).read_text(encoding="utf-8"))
    assert report["cad_test"]["reason"] == "artifact_selection_unavailable"
    assert "cad_test_seal" not in report


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
async def test_sealed_benchmark_integrity_failure_is_recorded_as_blocked_evidence(
    tmp_path, monkeypatch
):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _session(tmp_path, source_kind="benchmark", source_id="P01")
    attached = []

    async def download(_key, destination, *, expected_size=None):
        Path(destination).write_bytes(b"tampered")
        return {"sha256": "0" * 64, "size_bytes": 8}

    def attach(payload, result, **kwargs):
        attached.append((payload, result, kwargs))

    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
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

    assert len(attached) == 1
    payload, result, kwargs = attached[0]
    assert payload == {
        "workflow_run_id": "workflow-1",
        "candidate_revision_id": "revision-1",
    }
    assert result["status"] == "blocked"
    assert result["reason"] == "artifact_integrity_mismatch"
    assert result["case_id"] == "P01"
    assert result["artifact_kind"] == "step"
    assert kwargs["root"] == tmp_path


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
async def test_sealed_benchmark_candidate_records_final_attachment_failure(
    tmp_path, monkeypatch
):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _session(tmp_path, source_kind="benchmark", source_id="P01")
    source = {"step": b"step-artifact"}
    failures = []

    async def download(key, destination, *, expected_size=None):
        data = source[key]
        Path(destination).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    def attach(*args, **kwargs):
        raise RuntimeError("attachment failed")

    def record(payload, result, **kwargs):
        failures.append((payload, result, kwargs))

    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_artifacts", attach)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_observation", record)

    await observe_sealed_candidate(
        {"workflow_run_id": "workflow-1"},
        {
            "status": "succeeded",
            "candidate_revision_id": "revision-1",
            "artifacts": [
                {
                    "artifact_kind": "step",
                    "object_key": "step",
                    "size_bytes": len(source["step"]),
                    "sha256": hashlib.sha256(source["step"]).hexdigest(),
                }
            ],
        },
        root=tmp_path,
        config=CadTestRunnerConfig(root=tmp_path, scorer_image="test-image"),
    )

    assert len(failures) == 1
    payload, result, kwargs = failures[0]
    assert payload == {
        "workflow_run_id": "workflow-1",
        "candidate_revision_id": "revision-1",
    }
    assert result["status"] == "error"
    assert result["reason"] == "cad_test_attachment_failed"
    assert result["case_id"] == "P01"
    assert result["workflow_id"] == "workflow-1"
    assert result["candidate_revision_id"] == "revision-1"
    assert kwargs["root"] == tmp_path


@pytest.mark.asyncio
async def test_sealed_benchmark_reraises_when_failure_evidence_cannot_be_persisted(
    tmp_path, monkeypatch
):
    from app.evaluation.cad_test_observer import observe_sealed_candidate

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    _session(tmp_path, source_kind="benchmark", source_id="P01")
    source = {"step": b"step-artifact"}

    async def download(key, destination, *, expected_size=None):
        data = source[key]
        Path(destination).write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}

    def attach(*args, **kwargs):
        raise RuntimeError("attachment failed")

    def fail_record(*args, **kwargs):
        raise RuntimeError("failure evidence could not be stored")

    monkeypatch.setattr("app.evaluation.cad_test_observer.download_object", download)
    monkeypatch.setattr("app.evaluation.cad_test_observer.attach_cad_test_artifacts", attach)
    monkeypatch.setattr("app.evaluation.cad_test_observer._record_benchmark_observer_failure", fail_record)

    with pytest.raises(RuntimeError, match="failure evidence could not be stored"):
        await observe_sealed_candidate(
            {"workflow_run_id": "workflow-1"},
            {
                "status": "succeeded",
                "candidate_revision_id": "revision-1",
                "artifacts": [
                    {
                        "artifact_kind": "step",
                        "object_key": "step",
                        "size_bytes": len(source["step"]),
                        "sha256": hashlib.sha256(source["step"]).hexdigest(),
                    }
                ],
            },
            root=tmp_path,
            config=CadTestRunnerConfig(root=tmp_path, scorer_image="test-image"),
        )


@pytest.mark.asyncio
async def test_seal_activity_keeps_business_result_when_observation_is_dispatched(monkeypatch):
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

    monkeypatch.setattr("app.workflows.activities.dispatch_cad_test_observation", observer)
    activity_instance = McadWorkflowActivities.__new__(McadWorkflowActivities)

    result = await activity_instance._run_observed(
        "agent_v2.seal_candidate",
        {"workflow_run_id": "workflow-1"},
        lambda: _return_expected(expected),
    )

    assert result is expected
    assert observed == [({"workflow_run_id": "workflow-1"}, expected)]
    assert terminal == [({"workflow_run_id": "workflow-1"}, expected)]


@pytest.mark.asyncio
async def test_required_evaluation_does_not_swallow_activity_evidence_write_failure(monkeypatch):
    from temporalio.exceptions import ApplicationError
    from app.workflows.activities import McadWorkflowActivities

    monkeypatch.setenv("CAD_TEST_REQUIRED", "true")
    calls = []

    async def operation():
        calls.append("operation")
        return {"status": "succeeded"}

    def fail_recording(*args, **kwargs):
        raise OSError("evidence store unavailable")

    monkeypatch.setattr("app.workflows.activities.record_activity_result", fail_recording)
    activity_instance = McadWorkflowActivities.__new__(McadWorkflowActivities)

    with pytest.raises(ApplicationError) as error:
        await activity_instance._run_observed(
            "agent_v2.validate_geometry",
            {"workflow_run_id": "workflow-1"},
            operation,
        )

    assert error.value.non_retryable is True
    assert calls == ["operation"]


@pytest.mark.asyncio
async def test_required_evaluation_fails_closed_when_observation_dispatch_fails(monkeypatch):
    from temporalio.exceptions import ApplicationError
    from app.workflows.activities import McadWorkflowActivities

    monkeypatch.setenv("CAD_TEST_REQUIRED", "true")
    monkeypatch.setattr("app.workflows.activities.record_activity_result", lambda *a, **k: None)
    monkeypatch.setattr("app.workflows.activities.record_workflow_terminal", lambda *a, **k: None)

    async def fail_dispatch(*args, **kwargs):
        raise OSError("Temporal observation dispatch unavailable")

    monkeypatch.setattr("app.workflows.activities.dispatch_cad_test_observation", fail_dispatch)
    activity_instance = McadWorkflowActivities.__new__(McadWorkflowActivities)

    with pytest.raises(ApplicationError) as error:
        await activity_instance._run_observed(
            "agent_v2.seal_candidate",
            {"workflow_run_id": "workflow-1"},
            lambda: _return_expected({"status": "succeeded", "candidate_revision_id": "revision-1"}),
        )

    assert error.value.non_retryable is True


async def _return_expected(value):
    return value
