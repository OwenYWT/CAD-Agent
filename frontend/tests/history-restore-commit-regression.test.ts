import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const ROOT = join(import.meta.dirname, "..", "..");
const read = (path: string) => readFileSync(join(ROOT, path), "utf8");

test("committed historical restore synchronizes panel and durable state", () => {
  const store = read("frontend/src/stores/sessionStore.ts");
  const applyChangeSet = store.match(
    /applyDurableChangeSet: \(detail, panelId\) =>([\s\S]*?)\r?\n\r?\n\x20{2}setDurableTaskStatus:/,
  )?.[1] || "";

  assert.match(applyChangeSet, /result:/);
  assert.match(applyChangeSet, /preparedResult:/);
  assert.match(applyChangeSet, /const currentRevisionId =/);
  assert.match(applyChangeSet, /durableChangeSetHeadRevision\(detail\)/);
  assert.match(applyChangeSet, /panel\.result\?\.change_set_id === detail\.id/);
});

test("a stale document observation cannot replace the local committed head", () => {
  const socket = read("frontend/src/hooks/useWebSocket.ts");
  const identity = socket.match(
    /export function durableIdentityPayload\(panel: PanelState\) \{([\s\S]*?)\n\}/,
  )?.[1] || "";

  assert.match(identity, /currentRevisionId: doc\.head_revision_id/);
  assert.match(identity, /doc\.head_revision_id === durable\.currentRevisionId/);
  assert.match(identity, /: durable;/);
});

test("a terminal durable snapshot preserves the complete generated result", () => {
  const store = read("frontend/src/stores/sessionStore.ts");

  assert.match(store, /preparedResult: result\.success \|\| result\.needs_confirmation/);
  assert.match(store, /panel\.result\?\.workflow_run_id === snapshot\.id/);
});

test("an abandoned Change Set returns the panel to its base revision", () => {
  const store = read("frontend/src/stores/sessionStore.ts");

  assert.match(store, /rejected.*changes_requested.*rolled_back/);
  assert.match(store, /changeSetId:.*null/);
  assert.match(store, /revision_id: detail\.base_revision_id/);
});

test("live Change Set lifecycle events update the panel baseline", () => {
  const store = read("frontend/src/stores/sessionStore.ts");
  const applyEvent = store.match(
    /applyDurableEvent: \(event, panelId\) =>([\s\S]*?)\r?\n\r?\n  applyDurableChangeSet:/,
  )?.[1] || "";

  assert.match(applyEvent, /durableChangeSetEventState\(/);
  assert.match(applyEvent, /currentRevisionId/);
  assert.match(applyEvent, /candidate_status: changeSetEvent\.candidateStatus/);
  assert.match(applyEvent, /changeSetId: abandonedChangeSet/);
});

test("the first restore Change Set event binds when the snapshot has no Change Set yet", () => {
  const store = read("frontend/src/stores/sessionStore.ts");
  const applyEvent = store.match(
    /applyDurableEvent: \(event, panelId\) =>([\s\S]*?)\r?\n\r?\n  applyDurableChangeSet:/,
  )?.[1] || "";

  assert.match(applyEvent, /event\.workflow_run_id === durable\.workflowRunId/);
  assert.match(applyEvent, /!durable\.changeSetId/);
  assert.match(applyEvent, /!panel\.result\?\.change_set_id/);
  assert.match(applyEvent, /sameChangeSet \|\| unboundChangeSet/);
});
