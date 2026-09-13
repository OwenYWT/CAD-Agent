import assert from "node:assert/strict";
import test from "node:test";

import {
  durableEventStep,
  durableChangeSetHeadRevision,
  durableResultNeedsCommit,
  durableResultHeadRevision,
  durableResultTaskStatus,
  durableSnapshotHeadRevision,
  durableSnapshotReplayCursor,
  durableWriteIdentity,
  shouldApplyDurableEvent,
  webSocketAuthProtocol,
} from "../src/adapters/durableTaskAdapter.ts";
import type {
  DurableTaskEvent,
  DurableTaskSnapshot,
  GenerationResult,
} from "../src/types/index.ts";
import type { DurableChangeSetDetail } from "../src/types/engineering.ts";


const event: DurableTaskEvent = {
  id: "event-12",
  workflow_run_id: "workflow-1",
  sequence: 12,
  event_type: "attempt.failed",
  payload: { message: "STEP 导出失败", error_code: "user_code_error" },
  projection: {
    stage: "modeling",
    label: "执行建模",
    status: "failed",
    message: "STEP 导出失败",
    step_key: "model-main",
    step_kind: "agent_model",
    attempt_number: 2,
  },
  occurred_at: "2026-07-30T12:00:00Z",
};


test("reconnect cursor applies each persisted event at most once", () => {
  assert.equal(shouldApplyDurableEvent(11, event), true);
  assert.equal(shouldApplyDurableEvent(12, event), false);
  assert.equal(shouldApplyDurableEvent(13, event), false);
});

test('candidate requires commit', () => {
  assert.equal(durableResultNeedsCommit({
    change_set_id: 'change-1',
    revision_id: 'candidate-1',
  }, 'base-1'), true);
  assert.equal(durableResultNeedsCommit({
    change_set_id: 'change-1',
    revision_id: 'candidate-1',
  }, 'candidate-1'), false);
  assert.equal(durableResultNeedsCommit({
    revision_id: 'candidate-1',
  }, 'base-1'), false);
});


test("durable events retain the backend sequence and failure evidence", () => {
  const step = durableEventStep(event);
  assert.equal(step.step, "attempt.failed");
  assert.equal(step.message, "STEP 导出失败");
  assert.equal(step.status, "failed");
  assert.equal(step.stage_id, "modeling");
  assert.equal(step.attempt, 2);
  assert.equal(step.detail?.label, "执行建模");
  assert.equal(step.detail?.sequence, 12);
  assert.equal(step.detail?.error_code, "user_code_error");
});


test("advisory validation failures are warnings with immutable evidence", () => {
  const validation = durableEventStep({
    ...event,
    id: "event-13",
    sequence: 13,
    event_type: "agent.validation_evidence.recorded",
    payload: {
      gate: "dfm",
      mode: "advisory",
      outcome: "failed",
      evidence_id: "evidence-1",
      evidence_hash: "f".repeat(64),
    },
    projection: {
      stage: "validation",
      label: "DFM 检查",
      status: "warn",
      message: "DFM 检查发现风险",
      gate: "dfm",
      mode: "advisory",
      outcome: "failed",
      evidence_id: "evidence-1",
      evidence_hash: "f".repeat(64),
    },
  });
  assert.equal(validation.status, "warn");
  assert.equal(validation.step, "agent.validation_evidence.recorded");
  assert.equal(validation.detail?.gate, "dfm");
  assert.equal(validation.detail?.evidence_hash, "f".repeat(64));
});


test("write identity is sent only when project, branch, and base are complete", () => {
  assert.deepEqual(durableWriteIdentity({
    projectId: "project-1",
    branchId: null,
    currentRevisionId: "revision-1",
  }), {});

  const identity = durableWriteIdentity({
    projectId: "project-1",
    branchId: "branch-1",
    currentRevisionId: "revision-1",
  });
  assert.equal(identity.project_id, "project-1");
  assert.equal(identity.branch_id, "branch-1");
  assert.equal(identity.expected_base_revision_id, "revision-1");
  assert.equal(typeof identity.idempotency_key, "string");
  assert.ok(identity.idempotency_key.length > 0);
});


function snapshot(
  workflowId: string,
  changeStatus = "pending_review",
): DurableTaskSnapshot {
  return {
    id: workflowId,
    project_id: "project-1",
    requested_by_principal_id: "principal-1",
    kind: "generate",
    status: "waiting_confirmation",
    request_payload: {
      branch_id: "branch-1",
      expected_base_revision_id: "revision-1",
    },
    last_event_sequence: 9,
    cancellation_requested_at: null,
    error_code: null,
    error_message: null,
    error: null,
    created_at: "2026-07-30T12:00:00Z",
    started_at: "2026-07-30T12:00:01Z",
    updated_at: "2026-07-30T12:00:02Z",
    completed_at: null,
    steps: [],
    artifacts: [],
    change_set: {
      id: "change-1",
      status: changeStatus,
      base_revision_id: "revision-1",
      candidate_revision_id: "revision-2",
      objective: "加厚",
      updated_at: "2026-07-30T12:00:02Z",
    },
    agent: {
      current_stage: "review",
      current_step_key: null,
      current_step_kind: null,
      current_status: "reviewable",
      candidate_build_id: "candidate-1",
      candidate_status: "reviewable",
      repair_count: 1,
      plan: null,
      validations: [{
        evidence_id: "evidence-1",
        evidence_hash: "e".repeat(64),
        gate: "dfm",
        mode: "advisory",
        outcome: "failed",
        issues: ["壁厚不足"],
        violations: [],
      }],
      risk_summary: {
        status: "attention_required",
        issue_count: 1,
        items: [],
      },
    },
  };
}


test("a new workflow never inherits another workflow event cursor", () => {
  assert.equal(
    durableSnapshotReplayCursor("workflow-old", 42, snapshot("workflow-new")),
    0,
  );
  assert.equal(
    durableSnapshotReplayCursor("workflow-new", 7, snapshot("workflow-new")),
    7,
  );
});


test("candidate revision is not the branch head before commit", () => {
  assert.equal(
    durableSnapshotHeadRevision(snapshot("workflow-1"), null),
    "revision-1",
  );
  assert.equal(
    durableSnapshotHeadRevision(snapshot("workflow-1", "accepted"), null),
    "revision-1",
  );
  assert.equal(
    durableSnapshotHeadRevision(snapshot("workflow-1", "committed"), null),
    "revision-2",
  );
});


test("confirmation status is preserved and the socket token is encoded", () => {
  const result = {
    success: false,
    needs_confirmation: true,
    workflow_run_id: "workflow-1",
  } as GenerationResult;
  assert.equal(durableResultTaskStatus(result, null), "waiting_confirmation");
  const protocol = webSocketAuthProtocol("secret/token+value");
  assert.ok(protocol?.startsWith("cad-agent-auth."));
  assert.equal(protocol?.includes("secret"), false);
  assert.equal(protocol?.includes("/"), false);
  assert.equal(protocol?.includes("+"), false);
});


test("legacy snapshot IDs never masquerade as durable revision IDs", () => {
  assert.equal(
    durableResultHeadRevision({
      success: true,
      snapshot_id: "legacy-snapshot-id",
    }, null),
    null,
  );
  assert.equal(
    durableResultHeadRevision({
      success: true,
      snapshot_id: "legacy-snapshot-id",
      revision_id: "revision-2",
    }, "revision-1"),
    "revision-2",
  );
});


test("Change Set actions update the branch head used by the next write", () => {
  const detail = {
    status: "accepted",
    base_revision_id: "revision-1",
    candidate_revision_id: "revision-2",
  } as DurableChangeSetDetail;
  assert.equal(durableChangeSetHeadRevision(detail), "revision-1");
  detail.status = "rejected";
  assert.equal(durableChangeSetHeadRevision(detail), "revision-1");
  detail.status = "changes_requested";
  assert.equal(durableChangeSetHeadRevision(detail), "revision-1");
  detail.status = "committed";
  assert.equal(durableChangeSetHeadRevision(detail), "revision-2");
  detail.status = "rolled_back";
  assert.equal(durableChangeSetHeadRevision(detail), "revision-1");
});

test("an uncommitted result never advances or rewinds a known branch head", () => {
  const candidate = {
    success: true,
    revision_id: "candidate-2",
    expected_base_revision_id: "base-1",
    change_set_id: "change-1",
  } as GenerationResult;
  assert.equal(durableResultHeadRevision(candidate, "base-1"), "base-1");
  assert.equal(durableResultHeadRevision(candidate, "candidate-2"), "candidate-2");
  assert.equal(durableResultHeadRevision(candidate, null), "base-1");
  assert.equal(durableResultHeadRevision({ ...candidate, expected_base_revision_id: undefined }, null), null);
});
