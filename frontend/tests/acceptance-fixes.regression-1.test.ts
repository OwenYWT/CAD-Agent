import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { adaptValidation } from "../src/adapters/projectAdapter.ts";
import { buildChangeSet } from "../src/adapters/changeSetAdapter.ts";
import type { GenerationResult, ModelSnapshotDetail } from "../src/types/index.ts";

const source = (path: string) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");

test("Q06 protected inspector files use authenticated downloads with visible errors", () => {
  const inspector = source("components/workspace/WorkspaceInspector.tsx");
  assert.doesNotMatch(inspector, /href=\{url\}/);
  assert.match(inspector, /await downloadEngineeringArtifact\(/);
  assert.match(inspector, /role="alert"/);
});

test("Q07 native gate summaries do not fabricate a non-watertight mesh", () => {
  const result = {
    success: true,
    validation: {
      status: "passed", issue_count: 0,
      gates: [
        { gate: "geometry", mode: "required", outcome: "passed" },
        { gate: "visual", mode: "advisory", outcome: "indeterminate" },
        { gate: "dfm", mode: "advisory", outcome: "failed" },
      ],
    },
  } as GenerationResult;
  const checks = adaptValidation(result);
  assert.equal(checks.some((check) => check.id === "mesh-watertight"), false);
  assert.equal(checks.find((check) => check.id === "gate-geometry")?.status, "pass");
  assert.equal(checks.find((check) => check.id === "gate-visual")?.status, "unknown");
  assert.equal(checks.find((check) => check.id === "gate-dfm")?.status, "fail");
});

test("Q07 actual legacy watertight evidence remains authoritative", () => {
  assert.equal(adaptValidation({ success: true, validation: { is_watertight: false } } as GenerationResult)[0]?.status, "fail");
  assert.equal(adaptValidation({ success: true, validation: { is_watertight: true } } as GenerationResult)[0]?.status, "pass");
});

test("Q07 analysis accepts native artifact-only models without Python source", () => {
  const dialog = source("components/validation/ValidationDialog.tsx");
  const gate = dialog.match(/const canAnalyzeDesign = Boolean\(([\s\S]*?)\);/)?.[1] || "";
  assert.match(gate, /result\.files\?\.stl/);
  assert.doesNotMatch(gate, /result\.code\s*&&/);
});

test("Q16 active generation exposes cancellation connected to the durable hook", () => {
  const workspace = source("components/workspace/EngineeringWorkspace.tsx");
  const agent = source("components/agent/AgentPanel.tsx");
  assert.equal(/onCancel=\{cancelGeneration\}/.test(workspace), true);
  const card = source("components/agent/TaskCard.tsx");
  assert.match(agent, /onCancel=\{onCancel\}/);
  assert.match(card, /task.running && onCancel/);
  assert.match(card, /onClick=\{onCancel\}/);
  assert.match(card, /取消任务/);
});

test("Q07 a change set with only disabled gates is unknown, never passed", () => {
  const snapshot = { id: "native", panel_id: "panel", prompt: "", code: "", result: {
    success: true, validation: { gates: [{ gate: "visual", mode: "disabled", outcome: "disabled" }] },
  } } as ModelSnapshotDetail;
  assert.equal(buildChangeSet(snapshot, null).validation.status, "unknown");
});
