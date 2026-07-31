import assert from "node:assert/strict";
import test from "node:test";

import { adaptDurableChangeSet } from "../src/adapters/changeSetAdapter.ts";
import type { DurableChangeSetDetail } from "../src/types/engineering.ts";


function durableDetail(): DurableChangeSetDetail {
  return {
    id: "change-1",
    project_id: "project-1",
    branch_id: "branch-1",
    branch_name: "main",
    base_revision_id: "revision-1",
    base_revision_number: 1,
    base_content_hash: "a".repeat(64),
    base_manifest: { schema_version: "mcad-revision-manifest.v1" },
    candidate_revision_id: "revision-2",
    candidate_revision_number: 2,
    candidate_content_hash: "b".repeat(64),
    candidate_manifest: {
      schema_version: "mcad-revision-manifest.v1",
      executions: [{
        step_key: "model",
        source_sha256: "c".repeat(64),
      }],
    },
    source_workflow_run_id: "workflow-1",
    objective: "将宽度改为 24 mm",
    change_summary: {
      modified_object_count: 1,
      parameter_changes: [{
        parameter_id: "width",
        label: "宽度",
        before: 20,
        after: 24,
        unit: "mm",
      }],
    },
    validation_summary: {
      status: "passed",
      issue_count: 0,
    },
    risk_summary: {
      level: "low",
      reasons: ["验证未发现问题"],
    },
    status: "pending_review",
    workflow_status: "waiting_confirmation",
    created_at: "2026-07-30T12:00:00Z",
    updated_at: "2026-07-30T12:01:00Z",
    base_artifacts: [],
    candidate_artifacts: [{
      id: "artifact-1",
      revision_id: "revision-2",
      artifact_kind: "step",
      filename: "result.step",
      content_type: "model/step",
      size_bytes: 100,
      sha256: "d".repeat(64),
      download_url: "https://objects.example/result.step?signature=real",
      created_at: "2026-07-30T12:01:00Z",
    }],
    audit_log: [{
      id: "audit-1",
      action: "change_set.evidence_updated",
      payload: { status: "pending_review" },
      occurred_at: "2026-07-30T12:01:00Z",
    }],
  };
}


test("durable Change Set UI is derived only from persisted evidence", () => {
  const result = adaptDurableChangeSet(durableDetail(), "panel-1");

  assert.equal(result.source, "durable");
  assert.equal(result.reviewStatus, "pending_review");
  assert.equal(result.taskId, "workflow-1");
  assert.equal(result.parameterChanges[0].before, 20);
  assert.equal(result.parameterChanges[0].after, 24);
  assert.equal(result.validation.status, "pass");
  assert.equal(result.risk.level, "low");
  assert.equal(result.files[0].kind, "added");
  assert.equal(result.files[0].evidence, "sha256");
  assert.equal(result.files[0].afterUrl?.includes("signature=real"), true);
  assert.equal(result.geometry.status, "unknown");
  assert.equal(result.code.status, "unknown");
});


test("missing validation and risk evidence never become a success", () => {
  const detail = durableDetail();
  detail.validation_summary = {};
  detail.risk_summary = {};
  detail.candidate_artifacts = [];

  const result = adaptDurableChangeSet(detail, "panel-1");

  assert.equal(result.validation.status, "unknown");
  assert.equal(result.risk.level, "unknown");
  assert.deepEqual(result.files, []);
});
