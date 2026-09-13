import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const PROJECT_ROOT = join(import.meta.dirname, "..", "..");
const PANEL_TEXT = readFileSync(join(PROJECT_ROOT, "frontend/src/components/VersionHistoryPanel.tsx"), "utf8");

test("version history panel keeps restore and part-edit affordances readable", () => {
  assert.match(PANEL_TEXT, /onRestore: \(snapshot: ModelSnapshotDetail\)/);
  assert.match(PANEL_TEXT, /getModelSnapshot/);
  assert.match(PANEL_TEXT, /diffModelSnapshots/);
  assert.match(PANEL_TEXT, /零件与历史版本/);
  assert.match(PANEL_TEXT, /零件级修改/);
  assert.match(PANEL_TEXT, /修改选中零件/);
  assert.match(PANEL_TEXT, /原生版本从 FCStd 恢复/);
  assert.match(PANEL_TEXT, /对比当前/);
  assert.match(PANEL_TEXT, /恢复/);
});
