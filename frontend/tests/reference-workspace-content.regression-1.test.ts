import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND = join(import.meta.dirname, "..");
const read = (path: string) => readFileSync(join(FRONTEND, path), "utf8");

test("real projects open in the CAD editor instead of the process dashboard", () => {
  const workspace = read("src/components/workspace/EngineeringWorkspace.tsx");
  const sidebar = read("src/components/project/ProjectSidebar.tsx");

  assert.match(workspace, /useState<EngineeringDomain>\("mechanical"\)/);
  assert.match(workspace, /setView\("mechanical"\)/);
  assert.match(sidebar, /onNavigate\("mechanical"\)/);
});

test("reference workspace content is backed by real task and artifact evidence", () => {
  const agent = read("src/components/agent/AgentPanel.tsx");
  const inspector = read("src/components/workspace/WorkspaceInspector.tsx");
  const viewer = read("src/components/viewer/MechanicalWorkspace.tsx");

  assert.match(agent, /engineeringTaskEventLabel/);
  assert.match(agent, /ww-agent-artifact-card/);
  assert.match(inspector, /ww-inspector-export/);
  assert.match(viewer, /ww-viewer-statusbar/);
});

test("natural-language edits and durable confirmation use server-owned contracts", () => {
  const agent = read("src/components/agent/AgentPanel.tsx");
  const socket = read("src/hooks/useWebSocket.ts");
  const service = read("src/services/engineeringService.ts");

  assert.match(socket, /operation_intent: operationIntentForPanel\(panel\)/);
  assert.match(socket, /panel\?\.result\?\.success \? "modify" : "generate"/);
  assert.match(agent, /panel\.durable\?\.confirmation/);
  assert.match(agent, /confirmDurableTask/);
  assert.match(service, /\/confirmation`/);
  assert.match(service, /JSON\.stringify\(\{ accepted, note \}\)/);
});
