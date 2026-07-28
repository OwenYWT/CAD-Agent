import assert from "node:assert/strict";
import test from "node:test";

import { buildChangeSet } from "../src/adapters/changeSetAdapter.ts";
import type { ModelSnapshotDetail } from "../src/types/index.ts";


function snapshot(
  overrides: Partial<ModelSnapshotDetail>,
): ModelSnapshotDetail {
  return {
    id: "snapshot-current",
    panel_id: "panel-1",
    parent_snapshot_id: "snapshot-base",
    version: 2,
    source: "execute_code",
    prompt: "将宽度改为 24 mm",
    status: "pass",
    created_at: "2026-07-27T12:00:00Z",
    code: "width = 24\nresult = box(width, 10, 5)",
    result: {
      request_id: "request-current",
      task_id: "task-current",
      success: true,
      files: {
        step: "/api/files/request-current/result.step",
        stl: "/api/files/request-current/result.stl",
      },
      parameters: [
        {
          name: "width",
          value: 24,
          default_value: 20,
          unit: "mm",
          comment: "宽度",
        },
      ],
      validation: {
        is_watertight: true,
        volume: 1200,
      },
      inspect_report: {
        verdict: "pass",
        checks: [],
        available_exports: ["step", "stl"],
        repair_attempts: 0,
        source: "geometry_validator",
      },
    },
    ...overrides,
  };
}


test("change set uses only parent snapshot evidence", () => {
  const base = snapshot({
    id: "snapshot-base",
    parent_snapshot_id: null,
    version: 1,
    prompt: "创建长方体",
    code: "width = 20\nresult = box(width, 10, 5)",
    result: {
      request_id: "request-base",
      success: true,
      files: {
        step: "/api/files/request-base/result.step",
        stl: "/api/files/request-base/result.stl",
      },
      parameters: [
        {
          name: "width",
          value: 20,
          default_value: 20,
          unit: "mm",
          comment: "宽度",
        },
      ],
      validation: {
        is_watertight: true,
        volume: 1000,
      },
    },
  });
  const current = snapshot({});

  const changeSet = buildChangeSet(current, base);

  assert.equal(changeSet.baseRevisionId, "snapshot-base");
  assert.equal(changeSet.parameterChanges.length, 1);
  assert.deepEqual(changeSet.parameterChanges[0], {
    parameterId: "width",
    label: "宽度",
    before: 20,
    after: 24,
    unit: "mm",
  });
  assert.equal(changeSet.geometry.status, "changed");
  assert.equal(changeSet.geometry.metrics[0].label, "体积");
  assert.equal(changeSet.files.every((file) => file.evidence === "reference_only"), true);
  assert.equal(changeSet.validation.status, "pass");
  assert.equal(changeSet.risk.level, "low");
  assert.equal(changeSet.taskId, "task-current");
});


test("missing parent evidence remains unknown and never invents success", () => {
  const current = snapshot({
    parent_snapshot_id: null,
    result: {
      request_id: "request-current",
      success: true,
      files: {},
    },
  });

  const changeSet = buildChangeSet(current, null);

  assert.equal(changeSet.baseRevisionId, null);
  assert.equal(changeSet.parameterChanges.length, 0);
  assert.equal(changeSet.code.status, "unknown");
  assert.equal(changeSet.geometry.status, "unknown");
  assert.equal(changeSet.files.length, 0);
  assert.equal(changeSet.validation.status, "unknown");
  assert.equal(changeSet.risk.level, "unknown");
});


test("known backend evidence labels are localized without changing values", () => {
  const current = snapshot({
    prompt: "manual code execution",
    result: {
      ...snapshot({}).result,
      parameters: [
        {
          name: "width",
          display_name: "Width",
          value: 24,
          default_value: 24,
          unit: "mm",
        },
      ],
    },
  });
  const base = snapshot({
    id: "snapshot-base",
    parent_snapshot_id: null,
    version: 1,
    result: {
      ...snapshot({}).result,
      parameters: [
        {
          name: "width",
          display_name: "Width",
          value: 20,
          default_value: 20,
          unit: "mm",
        },
      ],
    },
  });

  const changeSet = buildChangeSet(current, base);

  assert.equal(changeSet.objective, "手动执行参数化建模代码");
  assert.equal(changeSet.parameterChanges[0].label, "宽度");
  assert.equal(changeSet.validation.summary, "检查报告：通过，来源 几何验证器");
});
