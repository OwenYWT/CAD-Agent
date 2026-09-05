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
  "????",
  "CAD ?????",
  "?????????",
  "????:",
  "\u8fdb\u5ea6\u6587\u6848\u5df2\u91cd\u5199",
  "\u6d41\u7a0b\u5df2\u6b63\u5e38",
  "\u672a\u53d1\u73b0\u7801\u4e71\u6b8b\u7559",
  "\u72b6\u6001:",
  "Generation completed",
  "Generation failed",
  "Design brief needs confirmation",
  "鈥",
  "鈫",
  "姝ラ",
  "鏈",
  "鏍￠",
  "\u93e2\u677f",
  "\u6fde\u6d93",
  "\u59dd\u6b63",
  "\u6fb6\u6d36",
  "\u95c6\u6735",
  "\u7480\ufe40",
  "\u6fb6\u590d",
  "\u93b5\u8f66",
  "\u7470\u55da",
  "\u6dc7\ue1ac",
  "\u95ee\u9898\u5217\u8868",
  "\u95ee\u9898\u6e05\u5355",
];

test("progress and thinking text has no mojibake remnants", () => {
  const failures: string[] = [];

  for (const file of FILES_TO_CHECK) {
    const text = readFileSync(join(PROJECT_ROOT, file), "utf8");
    for (const snippet of FORBIDDEN_SNIPPETS) {
      if (text.includes(snippet)) failures.push(`${file}: ${snippet}`);
    }
  }

  assert.deepEqual(failures, []);
});
