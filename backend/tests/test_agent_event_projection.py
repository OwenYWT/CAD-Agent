from uuid import uuid4

import pytest

from app.execution.canonical import canonical_sha256
from app.services.event_relay import (
    _confirmation_projection,
    _project_task_bom,
    _project_task_error,
    _project_candidate_lifecycle,
    project_task_events,
)


def _event(sequence: int, event_type: str, payload: dict):
    return {
        "id": uuid4(),
        "workflow_run_id": uuid4(),
        "sequence": sequence,
        "event_type": event_type,
        "payload": payload,
        "occurred_at": "2026-08-12T00:00:00Z",
    }


def test_projection_preserves_order_and_maps_repair_and_validation():
    step_id = uuid4()
    attempt_id = uuid4()
    events = project_task_events(
        [
            _event(4, "agent.repair.source_generated", {
                "repair_step_key": "repair-model-main-01",
            }),
            _event(5, "agent.validation_evidence.recorded", {
                "gate": "dfm",
                "mode": "advisory",
                "outcome": "failed",
                "evidence_id": str(uuid4()),
                "evidence_hash": "a" * 64,
            }),
            _event(6, "attempt.state_changed", {
                "attempt_id": str(attempt_id),
                "status": "failed",
                "error_code": "user_code_failed",
                "error_message": "参数尺寸无效",
            }),
        ],
        step_rows=[{
            "id": step_id,
            "step_key": "model-main",
            "kind": "agent_model",
        }],
        attempt_rows=[{
            "id": attempt_id,
            "step_run_id": step_id,
            "attempt_number": 2,
        }],
    )
    assert [item["sequence"] for item in events] == [4, 5, 6]
    assert events[0]["projection"]["stage"] == "repair"
    assert events[1]["projection"]["status"] == "warn"
    assert events[1]["projection"]["evidence_hash"] == "a" * 64
    assert events[2]["projection"]["stage"] == "modeling"
    assert events[2]["projection"]["status"] == "failed"
    assert events[2]["projection"]["message"] == "参数尺寸无效"
    assert events[2]["projection"]["attempt_number"] == 2


def test_required_validation_failure_is_not_downgraded_to_warning():
    event = project_task_events(
        [_event(1, "agent.validation_evidence.recorded", {
            "gate": "geometry",
            "mode": "required",
            "outcome": "indeterminate",
            "evidence_id": str(uuid4()),
            "evidence_hash": "b" * 64,
        })],
        step_rows=[],
        attempt_rows=[],
    )[0]
    assert event["projection"]["status"] == "failed"
    assert event["projection"]["message"] == "几何检查未能判定"


def _plan():
    return {
        "schema_version": "durable-agent-plan.v1",
        "objective": "将孔径改为 8 mm",
        "operation": "modify",
        "model_kind": "simple",
        "modeling_strategy": "feature_edit",
        "design_brief": {
            "intent_summary": "修改孔径",
            "artifact_type": "part",
            "manufacturing_posture": "machinable",
            "assumptions": [],
            "critical_dimensions": [],
            "functional_requirements": [],
            "printability_targets": [],
            "acceptance_criteria": [],
            "open_questions": [],
        },
        "expected_base_revision_id": str(uuid4()),
        "affected_objects": [{
            "object_id": "hole",
            "object_type": "feature",
            "label": "安装孔",
            "change": "modify",
        }],
        "steps": [{
            "step_key": "modify-hole",
            "kind": "modify",
            "description": "修改孔径",
            "affected_object_ids": ["hole"],
        }],
        "confirmation_policy": "required",
        "confirmation_reason": "将修改现有特征。",
        "validation_policy": {
            "artifact_integrity": {"mode": "required", "repair_budget": 0},
            "geometry": {"mode": "required", "repair_budget": 2},
            "visual": {"mode": "advisory", "repair_budget": 1},
            "dfm": {"mode": "advisory", "repair_budget": 0},
        },
    }


def test_confirmation_projection_is_derived_from_persisted_plan():
    workflow_id = uuid4()
    plan = _plan()
    projection = _confirmation_projection(
        workflow_status="waiting_confirmation",
        workflow_run_id=workflow_id,
        plan_event_payload={
            "plan": plan,
            "requires_confirmation": True,
            "confirmation_reason": "将修改现有特征。",
        },
        workflow_kind="mcad.agent.v2.modify",
    )

    assert projection == {
        "status": "waiting",
        "workflow_run_id": workflow_id,
        "reason": "将修改现有特征。",
        "plan_hash": canonical_sha256(plan),
        "affected_objects": plan["affected_objects"],
    }


def test_confirmation_projection_fails_closed_for_missing_agent_plan():
    with pytest.raises(RuntimeError, match="confirmation_projection_invalid"):
        _confirmation_projection(
            workflow_status="waiting_confirmation",
            workflow_run_id=uuid4(),
            plan_event_payload=None,
            workflow_kind="mcad.agent.v2.modify",
        )


def test_terminal_task_error_prefers_latest_failed_attempt_structured_error():
    error = _project_task_error(
        workflow_status="failed",
        steps=[
            {
                "step_index": 1,
                "status": "failed",
                "updated_at": "2026-08-12T00:00:01Z",
                "error_code": "old_failure",
                "error_message": "old",
                "error_details": {},
                "attempts": [],
            },
            {
                "step_index": 2,
                "status": "failed",
                "updated_at": "2026-08-12T00:00:02Z",
                "error_code": "flattened",
                "error_message": "flattened",
                "error_details": {},
                "attempts": [{
                    "attempt_number": 2,
                    "status": "failed",
                    "error_details": {
                        "category": "validation",
                        "code": "sketch_conflicting_constraints",
                        "message": "Sketch constraints conflict",
                        "operation_id": "constraint-04",
                        "action": "sketch.add_constraint",
                        "details": {"solver_status": -3},
                        "retryable": False,
                        "evidence": {},
                    },
                }],
            },
        ],
    )
    assert error is not None
    assert error["code"] == "sketch_conflicting_constraints"
    assert error["operation_id"] == "constraint-04"
    assert error["details"] == {"solver_status": -3}


def test_task_error_ignores_repaired_failures_and_synthesizes_legacy_timeout():
    assert _project_task_error(
        workflow_status="succeeded",
        steps=[{
            "step_index": 1,
            "status": "failed",
            "updated_at": "2026-08-12T00:00:01Z",
            "error_code": "old_failure",
            "error_message": "old",
            "attempts": [],
        }],
    ) is None
    error = _project_task_error(
        workflow_status="timed_out",
        steps=[{
            "step_index": 1,
            "status": "timed_out",
            "updated_at": "2026-08-12T00:00:01Z",
            "error_code": "activity_timeout",
            "error_message": "Activity timed out",
            "error_details": {},
            "attempts": [],
        }],
    )
    assert error is not None
    assert error["category"] == "timeout"
    assert error["retryable"] is False


def test_bom_projection_requires_committed_artifacts_and_preserves_error():
    failed = _project_task_bom(
        workflow_status="failed",
        plan_payload={"model_kind": "assembly"},
        steps=[{
            "kind": "agent_bom",
            "status": "failed",
            "error": {
                "category": "infrastructure",
                "code": "bom_runtime_unsupported",
                "message": "Assembly module unavailable",
                "details": {},
                "retryable": False,
                "evidence": {},
            },
        }],
        artifacts=[],
        change_set=None,
        validation_rows=[],
    )
    assert failed is not None
    assert failed["status"] == "unsupported"
    assert failed["error"]["code"] == "bom_runtime_unsupported"

    workflow_id = uuid4()
    revision_id = uuid4()
    evidence_id = uuid4()
    succeeded = _project_task_bom(
        workflow_status="succeeded",
        plan_payload={"model_kind": "assembly"},
        steps=[{"kind": "agent_bom", "status": "succeeded"}],
        artifacts=[
            {
                "artifact_kind": "bom_json",
                "workflow_run_id": workflow_id,
                "filename": "bom.json",
            },
            {
                "artifact_kind": "bom_csv",
                "workflow_run_id": workflow_id,
                "filename": "bom.csv",
            },
        ],
        change_set={"candidate_revision_id": revision_id},
        validation_rows=[{
            "id": evidence_id,
            "gate": "bom",
            "outcome": "passed",
        }],
    )
    assert succeeded == {
        "status": "succeeded",
        "revision_id": revision_id,
        "evidence_id": evidence_id,
        "json_download_url": f"/api/files/{workflow_id}/bom.json",
        "csv_download_url": f"/api/files/{workflow_id}/bom.csv",
        "error": None,
    }


def test_bom_projection_is_not_applicable_to_part_plan():
    projection = _project_task_bom(
        workflow_status="succeeded",
        plan_payload={"model_kind": "simple"},
        steps=[],
        artifacts=[],
        change_set=None,
        validation_rows=[],
    )
    assert projection is not None
    assert projection["status"] == "not_applicable"


def test_candidate_projection_follows_change_set_lifecycle():
    assert _project_candidate_lifecycle(
        candidate_status="reviewable",
        change_set_status="pending_review",
        workflow_status="succeeded",
    ) == {
        "current_stage": "review",
        "current_status": "reviewable",
        "candidate_status": "reviewable",
    }
    assert _project_candidate_lifecycle(
        candidate_status="reviewable",
        change_set_status="accepted",
        workflow_status="succeeded",
    ) == {
        "current_stage": "review",
        "current_status": "accepted",
        "candidate_status": "accepted",
    }
    assert _project_candidate_lifecycle(
        candidate_status="reviewable",
        change_set_status="committed",
        workflow_status="succeeded",
    ) == {
        "current_stage": "complete",
        "current_status": "succeeded",
        "candidate_status": None,
    }


def test_change_set_events_project_terminal_review_feedback():
    events = project_task_events(
        [
            _event(1, "change_set.accepted", {
                "change_set_id": str(uuid4()),
                "status": "accepted",
            }),
            _event(2, "change_set.committed", {
                "change_set_id": str(uuid4()),
                "status": "committed",
            }),
        ],
        step_rows=[],
        attempt_rows=[],
    )
    assert events[0]["projection"]["stage"] == "review"
    assert events[0]["projection"]["status"] == "success"
    projection = events[1]["projection"]
    assert projection["stage"] == "complete"
    assert projection["label"] == "任务完成"
    assert projection["status"] == "success"
    assert projection["message"] == "版本已提交"
