import asyncio
import hashlib
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_seal_returns_before_scoring_and_dispatches_after_terminal(monkeypatch):
    from app.workflows import activities

    calls = []
    sealed = {"status": "succeeded", "candidate_revision_id": "rev"}

    async def seal():
        return sealed

    async def blocked_scorer(*args):
        await asyncio.Event().wait()

    async def dispatch(payload, result):
        calls.append(("dispatch", payload, result))

    monkeypatch.setattr(activities, "record_activity_result", lambda *args, **kw: None)
    monkeypatch.setattr(activities, "record_workflow_terminal", lambda *args: calls.append("terminal"))
    monkeypatch.setattr(activities, "observe_sealed_candidate", blocked_scorer, raising=False)
    monkeypatch.setattr(activities, "dispatch_cad_test_observation", dispatch, raising=False)
    worker = object.__new__(activities.McadWorkflowActivities)
    payload = {"workflow_run_id": "workflow"}

    result = await asyncio.wait_for(worker._run_observed("agent_v2.seal_candidate", payload, seal), 1)

    assert result is sealed
    assert calls == ["terminal", ("dispatch", payload, sealed)]


@pytest.mark.asyncio
async def test_seal_succeeds_when_observation_dispatch_fails(monkeypatch):
    from app.workflows import activities

    async def dispatch(*args):
        raise TimeoutError

    async def seal():
        return {"status": "succeeded"}

    monkeypatch.setattr(activities, "record_activity_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(activities, "record_workflow_terminal", lambda *args: None)
    monkeypatch.setattr(activities, "dispatch_cad_test_observation", dispatch)
    worker = object.__new__(activities.McadWorkflowActivities)
    assert await worker._run_observed("agent_v2.seal_candidate", {}, seal) == {"status": "succeeded"}


@pytest.mark.asyncio
async def test_dispatched_observation_survives_absent_worker_and_deduplicates(monkeypatch):
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.worker import Worker

    from app.config import settings
    from app.evaluation import cad_test_observer as observer
    from app.workflows.cad_test_observation import CadTestObservationWorkflow

    target = os.getenv("CAD_TEST_TEMPORAL_TEST_TARGET")
    if not target:
        pytest.skip("set CAD_TEST_TEMPORAL_TEST_TARGET to a test Temporal endpoint")
    client = await Client.connect(target)
    workflow_id = uuid4().hex
    queue = f"test-cadtest-observation-{workflow_id}"
    monkeypatch.setattr(settings, "temporal_agent_v2_task_queue", queue)
    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    monkeypatch.setattr(observer, "_report_source", lambda *args: {"kind": "user_task"})

    async def get_client():
        return client

    monkeypatch.setattr("app.temporal_client.get_temporal_client", get_client)
    payload = {"workflow_run_id": workflow_id}
    result = {"status": "succeeded", "candidate_revision_id": "revision"}
    await observer.dispatch_cad_test_observation(payload, result)
    await observer.dispatch_cad_test_observation(payload, result)
    handle = client.get_workflow_handle(f"cad-test:{workflow_id}:revision")
    observed = []

    @activity.defn(name="evaluation.observe_sealed_candidate")
    async def record(observation: dict) -> None:
        observed.append(observation)

    async with Worker(client, task_queue=queue, workflows=[CadTestObservationWorkflow], activities=[record]):
        await asyncio.wait_for(handle.result(), 20)
        await observer.dispatch_cad_test_observation(payload, result)

    assert [item["payload"] for item in observed] == [payload]
    assert observed[0]["result"]["candidate_revision_id"] == "revision"


@pytest.mark.asyncio
@pytest.mark.parametrize("score_status", ["passed", "failed", "error", "blocked"])
async def test_strict_wait_ignores_finished_business_report_until_scored(tmp_path, score_status):
    from benchmark.live import _wait_for_report

    path = tmp_path / "report.json"
    path.write_text('{"status":"finished","cad_test":{"status":"pending"}}')
    polling = asyncio.create_task(_wait_for_report(path, timeout_seconds=1, poll_seconds=0.001, cad_test_required=True))
    await asyncio.sleep(0.01)
    assert not polling.done()
    path.write_text(json.dumps({"status": "finished", "cad_test": {"status": score_status}}))
    assert (await polling)["cad_test"]["status"] == score_status


@pytest.mark.asyncio
async def test_strict_wait_timeout_preserves_finished_business_report(tmp_path):
    from benchmark.live import _wait_for_report, benchmark_row_from_live_report

    path = tmp_path / "report.json"
    path.write_text(json.dumps({"status": "finished"}))
    snapshot = await _wait_for_report(path, timeout_seconds=0.02, poll_seconds=0.001, cad_test_required=True)
    row = benchmark_row_from_live_report(
        case={"id": "case"}, response=SimpleNamespace(success=True), live_report=snapshot, report_path=path,
    )
    assert row["live_report_status"] == "finished"
    assert row["cad_test_gate_status"] == "unavailable"
    assert row["passed"] is False


def test_failed_live_report_preserves_workflow_error_instead_of_timeout(tmp_path):
    from benchmark.live import benchmark_row_from_live_report

    request_error = 'output_formats.2 rejected fcstd by stale Worker schema'
    row = benchmark_row_from_live_report(
        case={'id': 'case'},
        response=SimpleNamespace(success=False),
        live_report={
            'status': 'failed',
            'timeline': [{
                'event_type': 'operation_failed',
                'payload': {'error_type': 'ValidationError', 'error_message': request_error},
            }],
        },
        report_path=tmp_path / 'failed-report.json',
    )

    assert row['evaluation_status'] == 'unavailable'
    assert row['evaluation_reason'] == 'live_report_failed'
    assert row['live_report_error'] == request_error
    assert row['evaluation_reason'] != 'cad_test_observation_timeout'
    assert row['model_verdict'] is None
    assert row['passed'] is False


def test_legacy_live_uses_shared_root_without_requiring_cadtest(tmp_path, monkeypatch):
    from benchmark.eval import _legacy_live_inputs

    monkeypatch.setenv("CAD_EVALUATION_REPORT_ROOT", str(tmp_path))
    original = [{"id": "P01"}]
    cases, root = _legacy_live_inputs(original)
    assert root == tmp_path
    assert cases == [{"id": "P01", "cad_test_required": False}]
    assert original == [{"id": "P01"}]


@pytest.mark.parametrize(
    "route,native,score,verdict,expected",
    [
        ("2d", None, "passed", True, True),
        ("2d", False, "failed", False, False),
        ("2d", False, "passed", True, False),
        ("2d", None, "passed", None, False),
        ("3d", None, "passed", True, False),
        ("3d", True, "passed", True, True),
    ],
)
def test_html_and_json_share_route_aware_cadtest_verdict(tmp_path, route, native, score, verdict, expected):
    from benchmark.cad_test_report import _strict_run_passed
    from benchmark.live import benchmark_row_from_live_report

    geometry = [{"format": "dxf", "parseable": True}] if route == "2d" else [
        {"format": "step", "parseable": True, "valid": True, "solid_count": 1},
        {"format": "stl", "parseable": True},
    ]
    cad_test = {"status": score}
    if verdict is not None:
        cad_test["verdict"] = verdict
    if native is not None:
        cad_test["delivery"] = {"status": "passed" if native else "failed"}
    row = benchmark_row_from_live_report(
        case={"id": "test", "path": route}, response=SimpleNamespace(success=True),
        live_report={"status": "finished", "cad_test": cad_test,
                     "evidence": [{"source_kind": "agent_v2.validate_geometry", "observed": {"artifacts": geometry}}],
                     "timeline": [{"event_type": "visual_validation_finished", "payload": {"status": "rendered"}}]},
        report_path=tmp_path / "report.json",
    )
    assert row["passed"] is expected
    assert _strict_run_passed(row) is expected


@pytest.mark.parametrize("reported_hash,expected", [(None, "evaluation_error"), ("wrong", "evaluation_error"), ("valid", "pass")])
def test_internal_step_scorer_requires_its_own_artifact_hash(tmp_path, monkeypatch, reported_hash, expected):
    from app.evaluation import cad_test_runner as runner

    monkeypatch.setenv("CAD_EVALUATION_MODE", "observe")
    artifact = tmp_path / "result.step"
    artifact.write_bytes(b"step")
    digest = hashlib.sha256(b"step").hexdigest()
    monkeypatch.setattr(runner.CadTestRunnerConfig, "resolved_case", lambda self, case_id: tmp_path)

    def scorer(*, artifact, case, output, config):
        result = {"passed": True, "scorer_error": False}
        if reported_hash is not None:
            result["artifact_sha256"] = digest if reported_hash == "valid" else reported_hash
        (output / "score.json").write_text(json.dumps(result))
        return 0, "", ""

    monkeypatch.setattr(runner, "_run_internal_scorer", scorer)
    result = runner.run_cad_test(
        case_id="case", artifact_path=artifact, workflow_id="workflow", candidate_revision_id="rev",
        config=runner.CadTestRunnerConfig(root=tmp_path, scorer_image="test"),
    )
    assert result["status"] == expected
