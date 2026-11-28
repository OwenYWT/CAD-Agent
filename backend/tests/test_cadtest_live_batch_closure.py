from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_live_3d_requests_full_native_delivery_bundle(tmp_path):
    from benchmark.live import LiveBenchmarkContext, run_live_case
    calls = []
    workflow_id = uuid4()

    async def submit(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(workflow_run_id=workflow_id, handle=object())

    async def wait(*args, **kwargs):
        return SimpleNamespace(success=True, needs_confirmation=False)

    context = LiveBenchmarkContext(
        principal=object(), project_id=uuid4(), branch_id=uuid4(),
        expected_base_revision_id=uuid4(), report_root=tmp_path, timeout_seconds=0.01,
        worker_build_id="build-20261128",
    )
    await run_live_case(
        {"id": "case", "description": "create a block", "path": "extrude_cut"},
        context=context, repeat_index=1, submitter=submit, waiter=wait,
    )
    assert set(calls[0]["output_formats"]) == {"step", "stl", "fcstd"}
    assert calls[0]["source_worker_build_id"] == "build-20261128"


def test_v2_live_request_contract_accepts_fcstd_delivery():
    from app.models.workflow_requests import McadAgentWorkflowV2Request

    request = McadAgentWorkflowV2Request.model_validate({
        "workflow_run_id": str(uuid4()), "tenant_id": str(uuid4()),
        "project_id": str(uuid4()), "principal_id": str(uuid4()),
        "branch_id": str(uuid4()), "expected_base_revision_id": str(uuid4()),
        "operation": "generate", "objective": "simple model",
        "output_formats": ["step", "stl", "fcstd"],
    })
    assert set(request.output_formats) == {"step", "stl", "fcstd"}


def test_native_generation_output_copy_preserves_requested_fcstd(tmp_path, monkeypatch):
    from app.config import settings
    from app.sandbox.output_files import copy_output_files

    work = tmp_path / "work"
    output = work / "output"
    output.mkdir(parents=True)
    (output / "model.FCStd").write_bytes(b"native")
    monkeypatch.setattr(settings, "file_storage_dir", str(tmp_path / "stored"))

    copied = copy_output_files(work, "request", ["fcstd", "step", "stl"])

    assert "fcstd" in copied
    assert (tmp_path / "stored" / "request" / "model.FCStd").read_bytes() == b"native"


@pytest.mark.asyncio
async def test_finished_generation_with_pending_cadtest_is_not_model_failure(tmp_path):
    from benchmark.live import LiveBenchmarkContext, _wait_for_report, benchmark_row_from_live_report, run_live_case
    from app.evaluation.integration import evaluation_run_id_for_workflow
    from app.evaluation.store import FileEvaluationStore

    path = tmp_path / "report.json"
    path.write_text(json.dumps({"status": "finished", "cad_test": {"status": "pending"}}))
    report = await _wait_for_report(path, timeout_seconds=0.01, poll_seconds=0.001, cad_test_required=True)
    row = benchmark_row_from_live_report(
        case={"id": "case", "path": "extrude_cut"},
        response=SimpleNamespace(success=True), live_report=report, report_path=path,
    )
    assert row["model_verdict"] is None
    assert row["evaluation_status"] == "pending"
    assert row["passed"] is False

    workflow_id = uuid4()
    report_path = FileEvaluationStore(tmp_path).path_for(evaluation_run_id_for_workflow(workflow_id))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({"status": "finished", "cad_test": {"status": "pending"}}))

    async def submit(*args, **kwargs):
        return SimpleNamespace(workflow_run_id=workflow_id, handle=object())

    async def wait(*args, **kwargs):
        assert kwargs["timeout_seconds"] == 0.01
        return SimpleNamespace(success=True, needs_confirmation=False)

    result = await run_live_case(
        {"id": "case", "description": "create a block", "path": "extrude_cut"},
        context=LiveBenchmarkContext(
            principal=object(), project_id=uuid4(), branch_id=uuid4(),
            expected_base_revision_id=uuid4(), report_root=tmp_path,
            timeout_seconds=0.01, cad_test_timeout_seconds=0.01,
        ),
        repeat_index=1, submitter=submit, waiter=wait,
    )
    assert result["evaluation_status"] == "unavailable"
    assert result["evaluation_reason"] == "cad_test_observation_timeout"
    assert result["model_verdict"] is None
    assert result["passed"] is False

    report_path.write_text(json.dumps({
        "status": "finished",
        "cad_test": {"status": "passed", "verdict": True, "model_verdict": True,
                     "delivery": {"status": "passed"}},
    }))
    terminal = await _wait_for_report(report_path, timeout_seconds=0.01, cad_test_required=True)
    assert terminal["cad_test"]["verdict"] is True


def test_unscored_runs_do_not_count_as_model_failures_or_zero_accuracy():
    from benchmark.metrics import aggregate_case, aggregate_summary

    runs = [{
        "executed": True, "passed": None, "model_verdict": None,
        "cad_test_verdict": None, "evaluation_status": "unavailable",
        "delivery_status": "unavailable", "wall_time_ms": 12,
        "attempts": 1, "process_evaluation": {"status": "measured"},
    } for _ in range(3)]
    agg = aggregate_case(runs)
    overall = aggregate_summary([{"id": "case", "runs": runs, "agg": agg}])["overall"]

    assert agg["pass@1"] == {"mean": None, "std": None, "n": 0, "total": 3}
    assert agg["pass@k"] is None
    assert agg["pass^k"] is None
    assert agg["model_verdict_status"]["failed"] == 0
    assert agg["evaluation_status"]["unavailable"] == 3
    assert overall["pass@k"] is None
    assert overall["model_verdict_status"]["failed"] == 0
    assert overall["candidate_counts"] == {
        "generated": 3, "scored": 0, "terminal_scored": 0, "blocked_or_unavailable": 3,
    }


def test_cadtest_preflight_rejects_missing_configuration(monkeypatch):
    from app.evaluation.cad_test_observer import validate_cad_test_worker_config

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    monkeypatch.delenv("CAD_TEST_DATASET_ROOT", raising=False)
    monkeypatch.delenv("CAD_TEST_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="CAD_TEST_DATASET_ROOT"):
        validate_cad_test_worker_config()


@pytest.mark.asyncio
async def test_normal_observe_worker_starts_without_cadtest_configuration(monkeypatch):
    from app.workers import workflow_worker

    class ReadyWorker:
        async def run(self):
            return None

    async def ready(*args, **kwargs):
        return None

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    monkeypatch.delenv("CAD_TEST_DATASET_ROOT", raising=False)
    monkeypatch.delenv("CAD_TEST_ROOT", raising=False)
    monkeypatch.setattr(type(workflow_worker.settings), "assert_sandbox_config_safe", lambda self: None)
    monkeypatch.setattr(
        type(workflow_worker.settings),
        "assert_durable_control_plane_config_safe",
        lambda self: None,
    )
    monkeypatch.setattr(workflow_worker, "database_readiness", ready)
    monkeypatch.setattr(workflow_worker, "dispatcher_readiness", ready)
    monkeypatch.setattr(workflow_worker, "object_store_readiness", ready)
    monkeypatch.setattr(
        workflow_worker,
        "get_execution_backend",
        lambda: SimpleNamespace(runtime_snapshot=lambda: None),
    )
    monkeypatch.setattr(workflow_worker, "get_temporal_client", ready)
    monkeypatch.setattr(workflow_worker, "build_workflow_worker", lambda *args, **kwargs: ReadyWorker())
    monkeypatch.setattr(workflow_worker, "build_agent_v2_workflow_worker", lambda *args, **kwargs: ReadyWorker())
    monkeypatch.setattr(workflow_worker, "run_dispatcher", ready)

    await workflow_worker.run_worker()


@pytest.mark.asyncio
async def test_required_cadtest_worker_preflights_before_service_startup(monkeypatch):
    from app.workers import workflow_worker

    monkeypatch.setenv("CAD_TEST_REQUIRED", "true")
    monkeypatch.setenv("TEMPORAL_WORKER_BUILD_ID", "build-20261128")
    calls = []

    def preflight():
        calls.append("preflight")
        raise RuntimeError("CAD_TEST_DATASET_ROOT is required")

    async def should_not_start(*args, **kwargs):
        raise AssertionError("service startup must wait for CADTest preflight")

    monkeypatch.setattr(workflow_worker, "validate_cad_test_worker_config", preflight)
    monkeypatch.setattr(workflow_worker, "database_readiness", should_not_start)

    with pytest.raises(RuntimeError, match="CAD_TEST_DATASET_ROOT"):
        await workflow_worker.run_worker()

    assert calls == ["preflight"]


@pytest.mark.asyncio
async def test_required_cadtest_worker_rejects_missing_build_id(monkeypatch):
    from app.workers import workflow_worker

    monkeypatch.setenv("CAD_TEST_REQUIRED", "true")
    monkeypatch.delenv("TEMPORAL_WORKER_BUILD_ID", raising=False)
    monkeypatch.setattr(
        workflow_worker,
        "validate_cad_test_worker_config",
        lambda: pytest.fail("preflight must not run without a version identity"),
    )

    with pytest.raises(RuntimeError, match="TEMPORAL_WORKER_BUILD_ID"):
        await workflow_worker.run_worker()


def test_temporal_workers_keep_build_identity_without_versioned_routing(monkeypatch):
    from app.workers import workflow_worker

    captured = []

    class CapturingWorker:
        def __init__(self, client, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(workflow_worker, "Worker", CapturingWorker)
    monkeypatch.setattr(workflow_worker, "ModelJobWorker", CapturingWorker)
    monkeypatch.setenv("TEMPORAL_WORKER_BUILD_ID", "build-20261128")

    workflow_worker.build_workflow_worker(object(), backend=object())
    workflow_worker.build_agent_v2_workflow_worker(object(), backend=object())

    assert len(captured) == 2
    assert all(item["build_id"] == "build-20261128" for item in captured)
    assert all(item.get("use_worker_versioning", False) is False for item in captured)


def test_observe_worker_preflights_dataset_image_report_root_and_native_runtime(tmp_path, monkeypatch):
    from app.evaluation import cad_test_observer as observer
    from app.evaluation.cad_test_runner import CadTestRunnerConfig

    report_root = tmp_path / "reports"
    dataset_root = tmp_path / "dataset"
    config = CadTestRunnerConfig(root=tmp_path / "scoring", dataset_root=dataset_root, scorer_image="test-image")
    monkeypatch.setattr(observer, "config_from_environment", lambda: config)
    monkeypatch.setattr(observer, "load_dataset", lambda *args, **kwargs: SimpleNamespace(cases=({"id": "case"},), dataset_hash="a" * 64))
    monkeypatch.setattr(observer, "default_report_root", lambda: report_root)
    monkeypatch.setattr(observer.shutil, "which", lambda name: "podman" if name == "podman" else None)
    invocations = []

    def run(command, **kwargs):
        invocations.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(observer.subprocess, "run", run)
    assert observer.validate_cad_test_worker_config() == "a" * 64
    assert report_root.is_dir()
    assert invocations[0][:3] == ["podman", "image", "exists"]
    assert invocations[1][0:2] == ["podman", "run"]
