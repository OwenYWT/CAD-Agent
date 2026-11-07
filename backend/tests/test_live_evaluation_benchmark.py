from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_live_benchmark_row_requires_a_finished_report():
    from benchmark.live import benchmark_row_from_live_report

    row = benchmark_row_from_live_report(
        case={"id": "P01", "description": "create a cube", "path": "extrude_cut"},
        response=SimpleNamespace(
            success=True,
            inspect_report=SimpleNamespace(printable=True, verdict="pass"),
        ),
        live_report={"status": "running"},
        report_path="reports/live/live-live-1.json",
    )

    assert row["live_evaluation_run_id"] == "live-live-1"
    assert row["live_report_status"] == "running"
    assert row["passed"] is False
    assert row["live_report_error"] == "live report is not finished"


def test_live_benchmark_row_maps_only_real_report_evidence_to_artifact_gate():
    from benchmark.live import benchmark_row_from_live_report

    row = benchmark_row_from_live_report(
        case={"id": "P01", "description": "create a cube", "path": "extrude_cut"},
        response=SimpleNamespace(
            success=True,
            inspect_report=SimpleNamespace(printable=True, verdict="pass"),
        ),
        live_report={
            "status": "finished",
            "evidence": [
                {
                    "source_kind": "agent_v2.validate_geometry",
                    "observed": {
                        "artifacts": [
                            {"format": "step", "parseable": True, "valid": True, "solid_count": 1},
                            {"format": "stl", "parseable": True, "valid": True, "solid_count": 1},
                        ]
                    },
                }
            ],
            "timeline": [
                {
                    "event_type": "visual_validation_finished",
                    "payload": {"status": "rendered"},
                }
            ],
        },
        report_path="reports/live/live-live-1.json",
    )

    assert row["artifact_gate"] == {
        "source": "live_report",
        "step_readable": True,
        "stl_readable": True,
        "geometry_nonempty": True,
        "visual_rendered": True,
        "passed": True,
    }
    assert "rendered_views" not in row["artifact_gate"]
    assert row["passed"] is True


def test_compare_accepts_live_artifact_gate_without_invented_render_view_count():
    from benchmark.compare import compare_reports

    base = {
        "meta": {
            "run_id": "base",
            "case_set_hash": "stable",
            "model": "m",
            "llm_provider": "p",
            "temperature": 0.2,
            "rag_enabled": True,
            "n_repeats": 1,
            "n_cases": 1,
            "concurrency": 1,
            "pipeline_deadline_s": 180.0,
            "sandbox_runtime": "podman",
            "runtime_identity": {"image_digest": "sha256:" + "a" * 64, "architecture": "amd64"},
        },
        "summary": {
            "overall": {
                "pass@1": {"mean": 1.0, "std": 0.0},
                "pass@k": 1.0,
                "pass^k": 1.0,
                "verdict_pass_rate": {"mean": 1.0, "std": 0.0},
                "printable_rate": {"mean": 1.0, "std": 0.0},
                "one_shot_rate": {"mean": 1.0, "std": 0.0},
                "avg_wall_ms": 1,
            }
        },
        "cases": [],
    }
    candidate = dict(base)
    candidate["meta"] = dict(base["meta"], run_id="candidate")
    candidate["cases"] = [{
        "id": "P01",
        "difficulty": "simple",
        "path": "extrude_cut",
        "agg": {"pass@1": {"mean": 1.0, "std": 0.0}},
        "runs": [{
            "executed": True,
            "passed": True,
            "artifact_gate": {
                "source": "live_report",
                "step_readable": True,
                "stl_readable": True,
                "geometry_nonempty": True,
                "visual_rendered": True,
                "passed": True,
            },
        }],
    }]
    candidate["summary"] = {"overall": dict(base["summary"]["overall"])}

    result = compare_reports(base, candidate)

    assert result["artifact_failures"] == []
    assert result["false_successes"] == []


@pytest.mark.asyncio
async def test_live_benchmark_admission_passes_case_identity_and_links_report(tmp_path):
    from benchmark.live import LiveBenchmarkContext, run_live_case

    calls = []
    workflow_id = uuid4()
    principal = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4())

    async def submit(principal_arg, **kwargs):
        calls.append((principal_arg, kwargs))
        return SimpleNamespace(workflow_run_id=workflow_id, handle=object())

    async def wait(_principal, _submission, *, timeout_seconds):
        assert timeout_seconds == 12
        return SimpleNamespace(
            success=True,
            workflow_run_id=workflow_id,
            request_id=str(workflow_id),
            attempts=1,
            validation=None,
            inspect_report=None,
            plan=None,
            dfm_analysis=None,
            error=None,
        )

    report_path = tmp_path / "live" / f"live_live-{workflow_id}.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "evaluation_run_id": f"live-{workflow_id}",
                "status": "finished",
                "finished_at": "2026-11-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    result = await run_live_case(
        {"id": "P01", "description": "create a cube", "path": "extrude_cut"},
        context=LiveBenchmarkContext(
            principal=principal,
            project_id=uuid4(),
            branch_id=uuid4(),
            expected_base_revision_id=uuid4(),
            report_root=tmp_path,
            timeout_seconds=12,
        ),
        repeat_index=1,
        submitter=submit,
        waiter=wait,
    )

    assert calls[0][0] is principal
    assert calls[0][1]["source_kind"] == "benchmark"
    assert calls[0][1]["source_id"] == "P01"
    assert result["live_evaluation_run_id"] == f"live-{workflow_id}"
    assert result["live_report_status"] == "finished"
    assert result["live_report_path"].endswith(
        f"live_live-{workflow_id}.json"
    )


@pytest.mark.asyncio
async def test_live_benchmark_accepts_real_confirmation_gate(tmp_path):
    from benchmark.live import LiveBenchmarkContext, run_live_case

    workflow_id = uuid4()
    principal = SimpleNamespace(tenant_id=uuid4(), principal_id=uuid4())
    confirmations = []
    responses = iter(
        [
            SimpleNamespace(needs_confirmation=True),
            SimpleNamespace(
                success=True,
                workflow_run_id=workflow_id,
                request_id=str(workflow_id),
                attempts=1,
                validation=None,
                inspect_report=None,
                plan=None,
                dfm_analysis=None,
                error=None,
            ),
        ]
    )

    async def submit(_principal, **_kwargs):
        return SimpleNamespace(workflow_run_id=workflow_id, handle=object())

    async def wait(_principal, _submission, *, timeout_seconds):
        return next(responses)

    async def confirm(workflow_run_id_arg, **kwargs):
        confirmations.append((workflow_run_id_arg, kwargs))

    report_path = tmp_path / "live" / f"live_live-{workflow_id}.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "evaluation_run_id": f"live-{workflow_id}",
                "status": "finished",
                "finished_at": "2026-11-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    await run_live_case(
        {"id": "P01", "description": "create a cube", "path": "extrude_cut"},
        context=LiveBenchmarkContext(
            principal=principal,
            project_id=uuid4(),
            branch_id=uuid4(),
            expected_base_revision_id=uuid4(),
            report_root=tmp_path,
            timeout_seconds=12,
        ),
        repeat_index=1,
        submitter=submit,
        waiter=wait,
        confirmer=confirm,
    )

    assert confirmations == [
        (
            workflow_id,
            {
                "accepted": True,
                "note": "benchmark live observation",
                "workflow_kind": "mcad.agent.v2.generate",
            },
        )
    ]


def test_live_benchmark_mode_is_opt_in(monkeypatch):
    from benchmark.eval import _parser

    args = _parser().parse_args(["--cases", "P01"])
    assert args.live_observe is False
    assert args.project_id is None
    assert args.branch_id is None
    assert args.base_revision_id is None


def test_live_benchmark_mode_rejects_parallel_runs():
    from benchmark.eval import _validate_live_arguments

    with pytest.raises(SystemExit, match="--live-observe requires --concurrency 1"):
        _validate_live_arguments(
            SimpleNamespace(
                live_observe=True,
                concurrency=2,
                project_id=uuid4(),
                branch_id=uuid4(),
                base_revision_id=uuid4(),
            )
        )


def test_live_benchmark_parser_accepts_document_id():
    from benchmark.eval import _parser

    document_id = uuid4()
    args = _parser().parse_args([
        "--live-observe",
        "--document-id",
        str(document_id),
    ])

    assert args.document_id == document_id


@pytest.mark.asyncio
async def test_live_benchmark_resolves_workspace_from_document_snapshot(monkeypatch):
    from benchmark import eval as evaluation

    document_id = uuid4()
    project_id = uuid4()
    branch_id = uuid4()
    revision_id = uuid4()
    principal = SimpleNamespace()

    async def snapshot(context, requested_document_id):
        assert context is principal
        assert requested_document_id == document_id
        return {
            "project_id": str(project_id),
            "active_branch_id": str(branch_id),
            "head_revision_id": str(revision_id),
        }

    monkeypatch.setattr(evaluation, "document_snapshot", snapshot)

    resolved = await evaluation._resolve_live_workspace(
        document_id=document_id,
        project_id=None,
        branch_id=None,
        base_revision_id=None,
        principal=principal,
    )

    assert resolved == (project_id, branch_id, revision_id)


@pytest.mark.asyncio
async def test_live_benchmark_rejects_snapshot_without_head_revision(monkeypatch):
    from benchmark import eval as evaluation

    async def snapshot(*_args, **_kwargs):
        return {
            "project_id": str(uuid4()),
            "active_branch_id": str(uuid4()),
            "head_revision_id": None,
        }

    monkeypatch.setattr(evaluation, "document_snapshot", snapshot)

    with pytest.raises(SystemExit, match="head_revision_id"):
        await evaluation._resolve_live_workspace(
            document_id=uuid4(),
            project_id=None,
            branch_id=None,
            base_revision_id=None,
            principal=SimpleNamespace(),
        )


def test_live_benchmark_accepts_legacy_workspace_arguments():
    from benchmark.eval import _validate_live_arguments

    _validate_live_arguments(
        SimpleNamespace(
            live_observe=True,
            concurrency=1,
            document_id=None,
            project_id=uuid4(),
            branch_id=uuid4(),
            base_revision_id=uuid4(),
        )
    )


def test_live_benchmark_rejects_mixed_workspace_sources():
    from benchmark.eval import _validate_live_arguments

    with pytest.raises(SystemExit, match="cannot combine"):
        _validate_live_arguments(
            SimpleNamespace(
                live_observe=True,
                concurrency=1,
                document_id=uuid4(),
                project_id=uuid4(),
                branch_id=uuid4(),
                base_revision_id=uuid4(),
            )
        )


def test_live_benchmark_rejects_missing_workspace_source():
    from benchmark.eval import _validate_live_arguments

    with pytest.raises(SystemExit, match="--document-id"):
        _validate_live_arguments(
            SimpleNamespace(
                live_observe=True,
                concurrency=1,
                document_id=None,
                project_id=None,
                branch_id=None,
                base_revision_id=None,
            )
        )
