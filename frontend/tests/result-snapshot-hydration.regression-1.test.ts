import assert from "node:assert/strict";
import test from "node:test";

import {
  hydrateGenerationResult,
  resultMatchesSnapshot,
} from "../src/adapters/resultSnapshotAdapter.ts";
import type {
  GenerationResult,
  ModelSnapshotDetail,
} from "../src/types/index.ts";

// Regression: ISSUE-001 — successful WebSocket results omitted editable parameters
// Found by /qa on 2026-08-25
// Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-08-25.md

const liveResult: GenerationResult = {
  success: true,
  request_id: "request-live",
  revision_id: "revision-live",
  code: "edge_mm = 30  # [10:1:100]\nresult = box(edge_mm)",
  files: {
    step: "/api/files/request-live/model-result.step",
    stl: "/api/files/request-live/model-result.stl",
  },
};

const snapshot: ModelSnapshotDetail = {
  id: "snapshot-live",
  panel_id: "panel-1",
  revision_id: "revision-live",
  parent_snapshot_id: "snapshot-base",
  version: 4,
  source: "execute_code",
  prompt: "执行用户提交的 MCAD 代码",
  status: "pass",
  created_at: "2026-08-25T14:00:00Z",
  code: liveResult.code || "",
  files: liveResult.files,
  parameters: [
    {
      name: "edge_mm",
      display_name: "Edge",
      value: 30,
      default_value: 30,
      min: 10,
      max: 100,
      step: 1,
      unit: "mm",
      group: "Body",
      comment: "边长",
    },
  ],
  result: {
    success: true,
    code: liveResult.code,
  },
};

test("matches an execute-code snapshot by immutable revision or exact code", () => {
  assert.equal(resultMatchesSnapshot(liveResult, snapshot), true);
});

test("hydrates only missing evidence while preserving the live task identity", () => {
  const hydrated = hydrateGenerationResult(liveResult, snapshot);

  assert.equal(hydrated.request_id, "request-live");
  assert.equal(hydrated.revision_id, "revision-live");
  assert.equal(hydrated.snapshot_id, "snapshot-live");
  assert.equal(hydrated.version, 4);
  assert.equal(hydrated.parameters?.[0]?.name, "edge_mm");
  assert.equal(hydrated.parameters?.[0]?.value, 30);
  assert.deepEqual(hydrated.files, liveResult.files);
});

test("never mixes evidence from an unrelated snapshot", () => {
  const unrelated: ModelSnapshotDetail = {
    ...snapshot,
    id: "snapshot-other",
    revision_id: "revision-other",
    code: "edge_mm = 20\nresult = box(edge_mm)",
  };

  assert.equal(resultMatchesSnapshot(liveResult, unrelated), false);
  assert.equal(hydrateGenerationResult(liveResult, unrelated), liveResult);
});
