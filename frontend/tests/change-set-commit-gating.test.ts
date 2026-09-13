import assert from "node:assert/strict";
import test from "node:test";

import {
  adaptDurableChangeSet,
  durableChangeSetAcceptanceState,
  durableChangeSetCanCommit,
} from "../src/adapters/changeSetAdapter.ts";
import {
  durableResultNeedsCommit,
  durableChangeSetEventState,
  durableSnapshotHeadRevision,
} from "../src/adapters/durableTaskAdapter.ts";
import type { DurableChangeSetDetail } from "../src/types/engineering.ts";


function detail(overrides: Partial<DurableChangeSetDetail> = {}): DurableChangeSetDetail {
  return {
    id: "change-1",
    project_id: "project-1",
    branch_id: "branch-1",
    branch_name: "main",
    base_revision_id: "base-1",
    base_revision_number: 1,
    base_content_hash: "a".repeat(64),
    base_manifest: {},
    candidate_revision_id: "candidate-1",
    candidate_revision_number: 2,
    candidate_content_hash: "b".repeat(64),
    candidate_manifest: {},
    source_workflow_run_id: "workflow-1",
    objective: "Update model",
    change_summary: {},
    validation_summary: {
      status: "passed",
      issue_count: 0,
      gates: [{ gate: "geometry", mode: "required", outcome: "passed" }],
    },
    risk_summary: {},
    status: "accepted",
    workflow_status: "succeeded",
    created_at: "2026-09-13T00:00:00Z",
    updated_at: "2026-09-13T00:00:00Z",
    base_artifacts: [],
    candidate_artifacts: [],
    audit_log: [],
    ...overrides,
  };
}

function changeSet(overrides: Partial<DurableChangeSetDetail> = {}) {
  return adaptDurableChangeSet(detail(overrides), "panel-1");
}


test("pending review acceptance reports actionable reasons without bypassing gates", () => {
  const pending = changeSet({ status: "pending_review" });
  assert.deepEqual(durableChangeSetAcceptanceState(pending), {
    canAccept: true,
    reason: null,
  });

  const warning = changeSet({
    status: "pending_review",
    validation_summary: {
      status: "warning",
      issue_count: 1,
      gates: [{ gate: "dfm", mode: "advisory", outcome: "failed" }],
    },
  });
  assert.deepEqual(durableChangeSetAcceptanceState(warning), {
    canAccept: false,
    reason: "review_note_required",
  });
  assert.deepEqual(durableChangeSetAcceptanceState(warning, "Reviewed the DFM risk"), {
    canAccept: true,
    reason: null,
  });

  assert.equal(
    durableChangeSetAcceptanceState(
      changeSet({
        status: "pending_review",
        validation_summary: { status: "failed", issue_count: 1 },
      }),
    ).reason,
    "validation_failed",
  );
  assert.equal(
    durableChangeSetAcceptanceState(
      changeSet({
        status: "pending_review",
        validation_summary: {},
      }),
    ).reason,
    "validation_unknown",
  );
  assert.equal(
    durableChangeSetAcceptanceState({
      source: "snapshot",
      reviewStatus: undefined,
      validation: pending.validation,
    }).reason,
    "not_durable",
  );
});


test("accepted Change Sets cannot commit while their workflow is active", () => {
  assert.equal(
    durableChangeSetCanCommit(changeSet({ workflow_status: "waiting_confirmation" })),
    false,
  );
  assert.equal(
    durableChangeSetCanCommit(changeSet({ workflow_status: "running" })),
    false,
  );
});


test("accepted Change Sets become committable only after successful workflow completion", () => {
  assert.equal(durableChangeSetCanCommit(changeSet()), true);
  assert.equal(
    durableChangeSetCanCommit(changeSet({ workflow_status: "failed" })),
    false,
  );
  assert.equal(
    durableChangeSetCanCommit(changeSet({ status: "pending_review" })),
    false,
  );
});


test("a committed candidate is the base for the next modification", () => {
  const committed = changeSet({ status: "committed" });
  const result = {
    change_set_id: committed.id,
    revision_id: committed.targetRevisionId,
  };
  assert.equal(
    durableResultNeedsCommit(result, committed.targetRevisionId),
    false,
  );
});


test("change-set details use the server branch head after commit or rollback", () => {
  assert.equal(
    durableSnapshotHeadRevision({
      id: "workflow-1",
      project_id: "project-1",
      kind: "mcad.agent.v2.modify",
      status: "succeeded",
      request_payload: {},
      last_event_sequence: 1,
      artifacts: [],
      change_set: {
        id: "change-1",
        status: "committed",
        base_revision_id: "base-1",
        candidate_revision_id: "candidate-1",
        head_revision_id: "candidate-2",
        objective: "Update model",
      },
    } as never, null),
    "candidate-2",
  );
});


test("a stale pending snapshot cannot roll a committed local head back", () => {
  const snapshot = {
    id: "workflow-1",
    project_id: "project-1",
    kind: "mcad.agent.v2.modify",
    status: "succeeded",
    request_payload: {},
    last_event_sequence: 1,
    artifacts: [],
    change_set: {
      id: "change-1",
      status: "pending_review",
      base_revision_id: "base-1",
      candidate_revision_id: "candidate-1",
      objective: "Update model",
    },
  } as never;
  assert.equal(
    durableSnapshotHeadRevision(snapshot, "candidate-1"),
    "candidate-1",
  );
});


test("a terminal snapshot follows the authoritative branch head", () => {
  const committed = {
    id: "workflow-1",
    project_id: "project-1",
    kind: "mcad.agent.v2.modify",
    status: "succeeded",
    request_payload: {},
    last_event_sequence: 1,
    artifacts: [],
    change_set: {
      id: "change-1",
      status: "committed",
      base_revision_id: "base-1",
      candidate_revision_id: "candidate-1",
      head_revision_id: "candidate-2",
      objective: "Update model",
    },
  } as never;
  assert.equal(
    durableSnapshotHeadRevision(committed, "candidate-1"),
    "candidate-2",
  );

  const rolledBack = {
    ...committed,
    change_set: {
      ...committed.change_set,
      status: "rolled_back",
      head_revision_id: "base-1",
    },
  } as never;
  assert.equal(
    durableSnapshotHeadRevision(rolledBack, "candidate-1"),
    "base-1",
  );
});

test("a committed Change Set event advances the live edit baseline", () => {
  assert.deepEqual(
    durableChangeSetEventState(
      {
        event_type: "change_set.committed",
        payload: {
          change_set_id: "change-1",
          status: "committed",
          base_revision_id: "base-1",
          candidate_revision_id: "candidate-1",
        },
      },
      "base-1",
    ),
    {
      changeSetId: "change-1",
      status: "committed",
      baseRevisionId: "base-1",
      candidateRevisionId: "candidate-1",
      currentRevisionId: "candidate-1",
      candidateStatus: null,
      currentStage: "complete",
    },
  );
});

test("a review rejection event returns the live edit baseline to base", () => {
  assert.deepEqual(
    durableChangeSetEventState(
      {
        event_type: "change_set.changes_requested",
        payload: {
          change_set_id: "change-1",
          status: "changes_requested",
          base_revision_id: "base-1",
          candidate_revision_id: "candidate-1",
        },
      },
      "base-1",
    ),
    {
      changeSetId: "change-1",
      status: "changes_requested",
      baseRevisionId: "base-1",
      candidateRevisionId: "candidate-1",
      currentRevisionId: "base-1",
      candidateStatus: null,
      currentStage: "complete",
    },
  );
});
