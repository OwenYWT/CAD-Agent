import assert from "node:assert/strict";
import test from "node:test";

import { engineeringTaskEventLabel } from "../src/utils/engineeringLabels.ts";


// Regression: ISSUE-001 — durable event names leaked into the Chinese task UI
// Found by /qa on 2026-08-01
// Report: .gstack/qa-reports/qa-report-localhost-m1-2026-08-01.md
test("known durable task events have Chinese presentation labels", () => {
  assert.equal(
    engineeringTaskEventLabel("workflow.state_changed"),
    "任务状态已更新",
  );
  assert.equal(
    engineeringTaskEventLabel("source.preparation_started"),
    "正在准备建模代码",
  );
  assert.equal(
    engineeringTaskEventLabel("validation.completed"),
    "工程验证已完成",
  );
});
