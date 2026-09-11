import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

// Regression: FR-003 — sidebar-footer account panel opened below the viewport
// Found by /qa on 2026-08-27
// Report: .gstack/qa-reports/frontend-fixes-20260827/

test("account panel anchors above the sidebar footer instead of below it", () => {
  const css = readFileSync(join(import.meta.dirname, "..", "src", "index.css"), "utf8");
  const rule = css.match(/\.ww-account-panel \{([\s\S]*?)\}/)?.[1] ?? "";

  assert.match(rule, /position:\s*fixed/);
  assert.match(rule, /bottom:\s*64px/);
  assert.doesNotMatch(rule, /top:\s*44px/);
});
