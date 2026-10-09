import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

// Regression: FR-004 — inspector existed at 1181px but was clipped by persisted desktop widths
// Found by /qa on 2026-08-27
// Report: .gstack/qa-reports/frontend-fixes-20260827/

test("intermediate desktop widths share Agent and inspector space", () => {
  const css = readFileSync(join(import.meta.dirname, "..", "src", "index.css"), "utf8");
  assert.match(css, /minmax\(0, 1fr\) 5px var\(--ww-right-width\)/);
  assert.doesNotMatch(css, /--ww-inspector-width|--ww-agent-width/);
});

test("mobile header retains four engineering entry points", () => {
  const css = readFileSync(join(import.meta.dirname, "..", "src", "index.css"), "utf8");
  const header = readFileSync(join(import.meta.dirname, "..", "src", "components", "project", "WorkspaceHeader.tsx"), "utf8");
  for (const label of ["属性", "检查", "版本"]) assert.ok(header.includes(`label: "${label}"`));
  assert.match(header, /aria-label="导出"/);
  assert.match(header, /aria-label="更多工具"[\s\S]*aria-expanded=\{menuOpen\}/);
  assert.match(header, /actions.map\(item => <button[\s\S]*onClick=\{\(\) => action\(item.run\)\}/);
  const narrow = css.match(/@media \(max-width: 479px\) \{([\s\S]*?)\n\}/)?.[1] ?? "";
  assert.doesNotMatch(narrow, /workspace-header__export-action/);
});

test("fixed mobile sidebar no longer reserves an empty grid track", () => {
  const css = readFileSync(join(import.meta.dirname, "..", "src", "index.css"), "utf8");
  const tablet = css.match(/@media \(max-width: 1279px\) \{([\s\S]*?)\n\}/)?.[1] ?? "";

  assert.match(tablet, /\.ww-app-shell\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
});
