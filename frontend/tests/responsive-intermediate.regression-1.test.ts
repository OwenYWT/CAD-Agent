import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

// Regression: FR-004 — inspector existed at 1181px but was clipped by persisted desktop widths
// Found by /qa on 2026-08-27
// Report: .gstack/qa-reports/frontend-fixes-20260827/

test("intermediate desktop widths cap both side panes without hiding the inspector", () => {
  const css = readFileSync(join(import.meta.dirname, "..", "src", "index.css"), "utf8");
  const media = css.match(/@media \(min-width: 1181px\) and \(max-width: 1279px\) \{([\s\S]*?)\n\}/)?.[1] ?? "";

  assert.match(media, /min\(var\(--ww-agent-width\), 340px\)/);
  assert.match(media, /min\(var\(--ww-inspector-width\), 280px\)/);
  assert.doesNotMatch(media, /display:\s*none/);
});

test("mobile header keeps preview and language controls reachable without redundant actions", () => {
  const css = readFileSync(join(import.meta.dirname, "..", "src", "index.css"), "utf8");
  const header = readFileSync(join(import.meta.dirname, "..", "src", "components", "project", "WorkspaceHeader.tsx"), "utf8");
  const mobile = css.match(/@media \(max-width: 760px\) \{([\s\S]*?)\n\}/)?.[1] ?? "";
  const narrow = css.match(/@media \(max-width: 479px\) \{([\s\S]*?)\n\}/)?.[1] ?? "";

  assert.match(header, /workspace-header__changes-action/);
  assert.match(header, /workspace-header__export-action/);
  assert.match(mobile, /workspace-header__preview-action[\s\S]*display:\s*inline-flex/);
  assert.match(mobile, /workspace-header__agent-action[\s\S]*workspace-header__changes-action[\s\S]*display:\s*none/);
  assert.match(narrow, /workspace-header__export-action[\s\S]*display:\s*none/);
});

test("fixed mobile sidebar no longer reserves an empty grid track", () => {
  const css = readFileSync(join(import.meta.dirname, "..", "src", "index.css"), "utf8");
  const tablet = css.match(/@media \(max-width: 1023px\) \{([\s\S]*?)\n\}/)?.[1] ?? "";

  assert.match(tablet, /\.ww-app-shell\s*\{[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\)/);
});
