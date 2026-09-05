import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND = join(import.meta.dirname, "..");

test("confirmed unreachable legacy frontend files stay removed", () => {
  const removed = [
    "src/assets/hero.png",
    "src/assets/react.svg",
    "src/assets/vite.svg",
    "src/components/DesignAnalysis.tsx",
    "src/components/MultiStepProgress.tsx",
  ];

  assert.deepEqual(removed.filter((path) => existsSync(join(FRONTEND, path))), []);
});

test("confirmed unused legacy selectors and dark browser chrome stay removed", () => {
  const css = readFileSync(join(FRONTEND, "src", "index.css"), "utf8");
  const html = readFileSync(join(FRONTEND, "index.html"), "utf8");

  for (const selector of [
    ".ww-pane-empty",
    ".workspace-button--ghost",
    ".start-history-overlay",
    ".workspace-progress-ring",
    ".workspace-switcher",
  ]) {
    assert.equal(css.includes(selector), false, selector);
  }
  assert.match(html, /name="theme-color" content="#f7f7f5"/);
});
