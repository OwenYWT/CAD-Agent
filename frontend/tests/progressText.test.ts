import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const PROJECT_ROOT = join(import.meta.dirname, "..", "..");

const FILES_TO_CHECK = [
  "frontend/src/stores/sessionStore.ts",
  "frontend/src/components/AgentRunTimeline.tsx",
  "backend/app/api/websocket.py",
  "backend/app/agent/multi_step.py",
  "backend/app/models/schemas.py",
  "backend/app/agent/orchestrator.py",
];

const FORBIDDEN_SNIPPETS = [
  /\?{4,}/,
  /CAD \?{5}/,
  /\?{9,}/,
  /Generation completed/,
  /Generation failed/,
  /Design brief needs confirmation/,
  /\uFFFD/,
  /join\(" \? "\)/,
  /queued: "\?"/,
  /running: "\?"/,
  /success: "\?"/,
  /failed: "\?"/,
  /skipped: "\?"/,
];

test("progress and thinking text has no mojibake remnants", () => {
  const failures: string[] = [];

  for (const file of FILES_TO_CHECK) {
    const text = readFileSync(join(PROJECT_ROOT, file), "utf8");
    for (const snippet of FORBIDDEN_SNIPPETS) {
      if (snippet.test(text)) failures.push(`${file}: ${snippet}`);
    }
  }

  assert.deepEqual(failures, []);
});
