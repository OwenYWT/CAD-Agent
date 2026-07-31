import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const PROJECT_ROOT = join(import.meta.dirname, "..", "..");
const STORE_TEXT = readFileSync(join(PROJECT_ROOT, "frontend/src/stores/sessionStore.ts"), "utf8");

function sliceBetween(text: string, start: string, end: string) {
  const startIndex = text.indexOf(start);
  const endIndex = text.indexOf(end, startIndex + start.length);
  assert.notEqual(startIndex, -1, `${start} should exist`);
  assert.notEqual(endIndex, -1, `${end} should exist after ${start}`);
  return text.slice(startIndex, endIndex);
}

test("resumable replay keeps the panel clickable instead of generating", () => {
  const runCreatedBlock = sliceBetween(STORE_TEXT, "setRunCreated: (run, panelId)", "setStep: (step, panelId)");
  const setStepBlock = sliceBetween(STORE_TEXT, "setStep: (step, panelId)", "setAgentStep: (agentStep, panelId)");

  assert.match(STORE_TEXT, /function isRunGenerating\(status\?: string \| null\) \{\s*return status === "running" \|\| status === "pending";/s);
  assert.match(STORE_TEXT, /if \(step\.step === "resume_available" \|\| step\.step === "complete" \|\| step\.step === "failed"\) return false;/);
  assert.match(runCreatedBlock, /const generating = isRunGenerating\(run\.status\);/);
  assert.match(setStepBlock, /const generating = isStepGenerating\(step\);/);
  assert.doesNotMatch(runCreatedBlock, /isGenerating: true/);
  assert.doesNotMatch(setStepBlock, /isGenerating: step !== null/);
});