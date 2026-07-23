import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const SRC = join(import.meta.dirname, "..", "src");

test("AI design review is gated until a generated STL exists", () => {
  const component = readFileSync(join(SRC, "components", "DesignAnalysis.tsx"), "utf8");
  const dialog = readFileSync(join(SRC, "components", "validation", "ValidationDialog.tsx"), "utf8");

  assert.match(component, /canAnalyze\?: boolean/);
  assert.match(component, /disabledReason\?: string/);
  assert.match(component, /if \(!hasResult \|\| !requestId \|\| is2D\) return null;/);
  assert.match(component, /if \(!canAnalyze\)/);
  assert.match(component, /生成 CAD 模型后才能进行 AI 设计审查/);
  assert.match(dialog, /const canAnalyzeDesign = Boolean\([\s\S]*result\.files\?\.stl[\s\S]*!result\.needs_confirmation/);
  assert.match(dialog, /disabled={!canAnalyzeDesign \|\| status === "loading"}/);
});