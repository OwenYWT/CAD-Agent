import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND = join(import.meta.dirname, "..");
const read = (path: string) => readFileSync(join(FRONTEND, path), "utf8");

test("FreeCAD parameters use the verified state hash and structured socket contract", () => {
  const drawer = read("src/components/parameters/ParameterDrawer.tsx");
  const socket = read("src/hooks/useWebSocket.ts");

  assert.match(drawer, /sourceParameters\.get\(parameter\.id\)\?\.source === "freecad"/);
  assert.match(drawer, /result\?\.parameter_state_sha256/);
  assert.match(drawer, /onModifyParameters/);
  assert.match(socket, /type: "modify_parameters"/);
  assert.match(socket, /expected_state_sha256/);
  assert.match(socket, /type: "modify_parameters",[\s\S]*updates,/);
});

test("the inspector exposes persisted native BOM states and never fabricates rows", () => {
  const inspector = read("src/components/workspace/WorkspaceInspector.tsx");
  const service = read("src/services/clients/revisions.ts");

  assert.match(inspector, /getRevisionBOM/);
  assert.match(inspector, /bomState === "unsupported"/);
  assert.match(inspector, /bomState === "stale_revision"/);
  assert.match(inspector, /bomDocument\.rows\.map/);
  assert.match(service, /revisions\/\$\{encodeURIComponent\(revisionId\)\}\/bom/);
  assert.doesNotMatch(inspector, /mock|placeholder|sampleBOM/i);
});

test("Agent failures render stable service codes and diagnostic fields", () => {
  const agent = read("src/components/agent/AgentPanel.tsx");

  assert.match(agent, /ERROR_CODE_LABELS/);
  const card = read("src/components/agent/TaskCard.tsx");
  assert.match(agent, /structuredErrorLabel\(task.errorCode\)/);
  assert.match(card, /errorDetails\.operation_id/);
  assert.match(card, /errorDetails\.constraint_status/);
  assert.doesNotMatch(agent, /includes\([^)]*(?:constraint|edge|parameter)/i);
});

test("history restoration hydrates the complete durable snapshot before review", () => {
  const service = read("src/services/clients/history.ts");
  const store = read("src/stores/sessionStore.ts");
  const sidebar = read("src/components/project/ProjectSidebar.tsx");

  assert.match(service, /workflowRunId\s*\?\s*getDurableTaskSnapshot\(workflowRunId\)/s);
  assert.match(service, /changeSetId:\s*taskSnapshot\?\.change_set\?\.id \|\| null/);
  assert.match(service, /taskSnapshot,\s*\n\s*};/);
  assert.match(store, /changeSetId:\s*p\.changeSetId \|\| result\?\.change_set_id \|\| null/);
  assert.match(sidebar, /if \(panel\.taskSnapshot\) applyDurableSnapshot\(panel\.taskSnapshot, panel\.id\)/);
});

test("WebGL QA mounts the production Viewer3D with a caller-supplied real artifact", () => {
  const harness = read("qa/viewer3d.tsx");

  assert.match(harness, /import Viewer3D from "\.\.\/src\/components\/Viewer3D"/);
  assert.match(harness, /URLSearchParams\(window\.location\.search\)/);
  assert.match(harness, /<Viewer3D stlUrl=\{artifactUrl\}/);
  assert.doesNotMatch(harness, /mock|fixture|placeholder/i);
});
