import assert from "node:assert/strict";
import test from "node:test";
import { useSessionStore } from "../src/stores/sessionStore.ts";
import { durableResultNeedsCommit, durableWriteIdentity } from "../src/adapters/durableTaskAdapter.ts";
import { taskState } from "../src/adapters/taskState.ts";
import type { DurableTaskSnapshot, DurableTaskEvent } from "../src/types/index.ts";
import type { DurableChangeSetDetail } from "../src/types/engineering.ts";

function setup(status = "pending_review", sequence = 2) {
  const store = useSessionStore.getState(); store.reset();
  const panel = store.getActivePanel();
  const snapshot = {id: "workflow", project_id: "project", kind: "mcad.agent.v2.modify", status: "succeeded",
    request_payload: {branch_id: "branch", expected_base_revision_id: "base"}, last_event_sequence: sequence,
    artifacts: [{artifact_kind: "step", download_url: "/candidate.step"}],
    change_set: {id: "change", status, base_revision_id: "base", candidate_revision_id: "candidate", head_revision_id: status === "committed" ? "candidate" : "base"},
  } as DurableTaskSnapshot;
  store.applyDurableSnapshot(snapshot, panel.id);
  return {store, panel, snapshot};
}
function review(status: string): DurableChangeSetDetail {
  return {id: "change", project_id: "project", branch_id: "branch", source_workflow_run_id: "workflow",
    base_revision_id: "base", candidate_revision_id: "candidate", status, workflow_status: "succeeded",
    head_revision_id: status === "committed" ? "candidate" : "base"} as DurableChangeSetDetail;
}
function event(status: string, sequence: number): DurableTaskEvent {
  return {id: status, workflow_run_id: "workflow", sequence, event_type: "change_set." + status,
    payload: {change_set_id: "change", base_revision_id: "base", candidate_revision_id: "candidate"}, occurred_at: new Date().toISOString()};
}

test("committed historical restore synchronizes result, prepared result and next baseline", () => {
  const {store, panel} = setup(); store.applyDurableChangeSet(review("committed"), panel.id);
  const current = store.getActivePanel();
  assert.equal(current.durable?.currentRevisionId, "candidate");
  assert.deepEqual(current.result, current.durable?.preparedResult);
  assert.equal(current.result?.files?.step, "/candidate.step");
  assert.equal(taskState(current).phase, "saved");
});

test("new writes use the observed document head and generation after a collaborator commits", () => {
  const identity = durableWriteIdentity({projectId: "project", branchId: "branch", currentRevisionId: "older-local-head"},
    {head_revision_id: "collaborator-head", state_version: 4});
  assert.equal(identity.expected_base_revision_id, "collaborator-head");
  assert.equal(identity.expected_state_version, 4); assert.ok(identity.idempotency_key);
});

test("a terminal snapshot preserves complete result data belonging to the same workflow", () => {
  const {store, panel, snapshot} = setup();
  store.setResult({...store.getActivePanel().result!, code: "source", assembly_parts: [{part_id: "part", name: "plate"}]}, panel.id);
  store.applyDurableSnapshot(snapshot, panel.id);
  assert.equal(store.getActivePanel().result?.code, "source");
  assert.deepEqual(store.getActivePanel().result?.assembly_parts, [{part_id: "part", name: "plate"}]);
});

test("abandoning a candidate preserves its artifact identity and unblocks editing the actual head", () => {
  for (const status of ["rejected", "changes_requested", "rolled_back"]) {
    const {store, panel} = setup(); store.applyDurableChangeSet({...review(status), head_revision_id: "newer-head"}, panel.id);
    const current = store.getActivePanel();
    assert.equal(current.durable?.currentRevisionId, "newer-head"); assert.equal(current.durable?.changeSetStatus, status);
    assert.equal(current.result?.revision_id, "candidate"); assert.equal(current.result?.change_set_id, "change");
    assert.equal(current.result?.files?.step, "/candidate.step");
    assert.equal(durableResultNeedsCommit(current.result, "newer-head", status), false);
    assert.equal(taskState(current).phase, "needs_input");
  }
});

test("live review events update task lifecycle while retaining candidate artifacts", () => {
  const {store, panel} = setup(); store.applyDurableEvent(event("rejected", 3), panel.id);
  const current = store.getActivePanel();
  assert.equal(current.durable?.changeSetStatus, "rejected"); assert.equal(current.durable?.currentRevisionId, "base");
  assert.equal(current.result?.revision_id, "candidate"); assert.equal(taskState(current).phase, "needs_input");
});

test("review replay cannot undo a newer committed or rejected snapshot", () => {
  for (const status of ["committed", "rejected"]) {
    const {store, panel} = setup(status, 8); store.applyDurableEvent(event("accepted", 3), panel.id);
    const current = store.getActivePanel();
    assert.equal(current.durable?.lastEventSequence, 3); assert.equal(current.durable?.changeSetStatus, status);
    assert.equal(taskState(current).phase, status === "committed" ? "saved" : "needs_input");
  }
});

test("a first restore review event binds before its next snapshot arrives", () => {
  const {store, snapshot} = setup(); store.reset();
  store.applyDurableSnapshot({...snapshot, change_set: undefined, last_event_sequence: 0});
  store.applyDurableEvent(event("accepted", 1));
  assert.equal(store.getActivePanel().durable?.changeSetId, "change"); assert.equal(store.getActivePanel().durable?.changeSetStatus, "accepted");
});

test("a committed historical result does not block modifying a later head", () => {
  const {store} = setup("committed");
  assert.equal(durableResultNeedsCommit(store.getActivePanel().result, "later-head", "committed"), false);
  assert.equal(durableResultNeedsCommit(store.getActivePanel().result, "later-head", "pending_review"), true);
});

test("a rejected local action cannot turn a successfully generated candidate or saved version into a failed task", () => {
  for (const status of ["pending_review", "committed"]) {
    const {store, panel} = setup(status);
    store.setError("请先审阅当前候选，尚未提交新的修改请求。", panel.id);
    const current = taskState(store.getActivePanel());
    assert.equal(current.phase, status === "committed" ? "saved" : "candidate");
    assert.equal(current.errorMessage, "");
    assert.equal(current.taskId, "workflow");
  }
});


test("review metadata never relabels the last valid model while a new candidate is still running", () => {
  for (const source of ["detail", "event"]) {
    const {store, panel} = setup("committed");
    const previous = store.getActivePanel().result;
    store.beginGeneration();
    store.setDurableWorkflowStarted({workflow_run_id: "new-workflow", project_id: "project", branch_id: "branch",
      expected_base_revision_id: "candidate", panel_id: panel.id, status: "running"});
    store.applyDurableSnapshot({id: "new-workflow", project_id: "project", kind: "mcad.modify", status: "waiting_confirmation",
      request_payload: {branch_id: "branch"}, last_event_sequence: 0,
      change_set: {id: "new-change", status: "pending_review", base_revision_id: "candidate", candidate_revision_id: "new-candidate"}} as DurableTaskSnapshot, panel.id);
    if (source === "detail") store.applyDurableChangeSet({...review("accepted"), id: "new-change",
      source_workflow_run_id: "new-workflow", candidate_revision_id: "new-candidate", workflow_status: "waiting_confirmation"}, panel.id);
    else store.applyDurableEvent({...event("accepted", 1), workflow_run_id: "new-workflow",
      payload: {change_set_id: "new-change", candidate_revision_id: "new-candidate", base_revision_id: "candidate"}}, panel.id);
    assert.deepEqual(store.getActivePanel().result, previous);
    assert.equal(store.getActivePanel().durable?.preparedResult, null);
    assert.equal(store.getActivePanel().durable?.changeSetStatus, "accepted");
  }
});
