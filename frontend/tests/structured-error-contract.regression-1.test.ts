import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";


// Regression: FUSION-006 — the UI mapped invented aliases instead of the
// structured codes emitted by the FreeCAD and BOM execution boundaries.

const agent = readFileSync(
  join(import.meta.dirname, "..", "src/components/agent/AgentPanel.tsx"),
  "utf8",
);

test("Agent UI labels canonical backend FreeCAD and BOM error codes", () => {
  for (const code of [
    "invalid_edge_selection_mode",
    "sketch_redundant_constraints",
    "sketch_conflicting_constraints",
    "sketch_under_constrained",
    "parameter_state_stale",
    "parameter_not_editable",
    "bom_runtime_unsupported",
    "subtractive_feature_no_effect",
  ]) {
    assert.match(agent, new RegExp(`\\b${code}:`), code);
  }
});

test("Agent UI no longer presents non-emitted FreeCAD aliases as contracts", () => {
  assert.doesNotMatch(
    agent,
    /freecad_(?:edge_selection|parameter|sketch)|bom_native_unsupported/,
  );
});
