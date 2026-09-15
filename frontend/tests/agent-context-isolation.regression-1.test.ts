import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("../src/components/workspace/EngineeringWorkspace.tsx", import.meta.url), "utf8");

// Wiring guard; the real browser regression also switches populated panels and
// opens a suggested repair from the mobile engineering-check dialog.
test("the shared Agent composer isolates local pending requests by owner, session and panel", () => {
  assert.match(workspace, /agentContextKey = `\$\{ownerId\}:\$\{sessionId\}:\$\{panel.id\}`/);
  assert.match(workspace, /<AgentPanel key=\{agentKey\}/);
  assert.doesNotMatch(workspace, /<AgentDrawer/);
});

test("explicit suggestions refresh even the embedded mobile composer and are panel scoped", () => {
  assert.match(workspace, /agentSuggestion\?\.contextKey === agentContextKey/);
  assert.match(workspace, /sequence: \(previous\?\.sequence \|\| 0\) \+ 1/);
  assert.match(workspace, /agentKey = `\$\{agentContextKey\}:\$\{activeSuggestion\?\.sequence \|\| 0\}`/);
});
