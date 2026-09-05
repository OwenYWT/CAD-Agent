import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const SRC = join(import.meta.dirname, "..", "src");

test("AI design review is gated until a generated STL exists", () => {
  const dialog = readFileSync(join(SRC, "components", "validation", "ValidationDialog.tsx"), "utf8");

  assert.match(dialog, /const canAnalyzeDesign = Boolean\([\s\S]*result\.files\?\.stl[\s\S]*!result\.needs_confirmation/);
  assert.match(dialog, /analyzeEngineeringResult\(/);
  assert.match(dialog, /disabled={!canAnalyzeDesign \|\| status === "loading"}/);
});
