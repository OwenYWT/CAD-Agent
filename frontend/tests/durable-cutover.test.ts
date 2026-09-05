import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";


const ROOT = join(import.meta.dirname, "..", "..");
const SOCKET = readFileSync(
  join(ROOT, "frontend/src/hooks/useWebSocket.ts"),
  "utf8",
);
const STORE = readFileSync(
  join(ROOT, "frontend/src/stores/sessionStore.ts"),
  "utf8",
);
const HISTORY = readFileSync(
  join(ROOT, "frontend/src/components/VersionHistoryPanel.tsx"),
  "utf8",
);
const CHANGE_SET = readFileSync(
  join(ROOT, "frontend/src/components/changes/ChangeSetDialog.tsx"),
  "utf8",
);
const ENGINEERING_SERVICE = readFileSync(
  join(ROOT, "frontend/src/services/engineeringService.ts"),
  "utf8",
);
const POSTGRES_HISTORY = readFileSync(
  join(ROOT, "backend/app/storage/postgres_history.py"),
  "utf8",
);


test("conversational writes and clarification carry durable workflow identity", () => {
  assert.match(SOCKET, /msg\.type === "task_submitted"/);
  assert.match(
    SOCKET,
    /workflow_run_id: panel\?\.durable\?\.workflowRunId \|\| undefined/,
  );
  assert.match(
    SOCKET,
    /type: "resume_run"[\s\S]*\.\.\.identity[\s\S]*idempotency_key:/,
  );
  assert.match(SOCKET, /code: panel\?\.result\?\.code \|\| ""/);
});


test("persisted source and immutable artifacts produce the terminal result", () => {
  assert.match(STORE, /event\.event_type === "source\.prepared"/);
  assert.match(STORE, /artifact\.download_url/);
  assert.match(STORE, /preparedResult:/);
  assert.match(STORE, /normalizePersistedPanel/);
  assert.match(STORE, /workflowRunId: \([\s\S]*p\.workflowRunId/);
  assert.match(
    STORE,
    /preparedResult: sameWorkflow[\s\S]*panel\.durable\?\.preparedResult/,
  );
  assert.match(STORE, /task_status: snapshot\.status/);
  assert.match(ENGINEERING_SERVICE, /active_workflow_run_id/);
});


test("a failed durable modification preserves the last successful model", () => {
  assert.match(
    STORE,
    /terminal[\s\S]*snapshot\.status !== "succeeded"[\s\S]*panel\.result\?\.success[\s\S]*\? panel\.result[\s\S]*: snapshotResult/,
  );
});


test("history snapshot details hydrate verified FreeCAD parameter state", () => {
  assert.match(POSTGRES_HISTORY, /read_verified_state_artifact/);
  assert.match(POSTGRES_HISTORY, /project_state_parameters/);
  assert.match(POSTGRES_HISTORY, /"parameter_state_sha256": state_sha256/);
  assert.match(
    SOCKET,
    /expectedStateSha256[\s\S]*panel\?\.result\?\.parameter_state_sha256/,
  );
});


test("version restore no longer calls the legacy branch-head writer", () => {
  assert.doesNotMatch(HISTORY, /restoreModelSnapshot/);
  assert.doesNotMatch(CHANGE_SET, /restoreModelSnapshot/);
  assert.match(HISTORY, /getModelSnapshot/);
  assert.match(
    HISTORY,
    /原生版本从 FCStd 恢复，源码版本重新执行已保存代码；生成候选后需审查提交/,
  );
});
