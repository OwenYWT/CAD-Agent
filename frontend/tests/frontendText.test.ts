import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND_SRC = join(import.meta.dirname, "..", "src");

const FILES_TO_CHECK = [
  "components/AgentRunTimeline.tsx",
  "components/RepairHistory.tsx",
  "components/VersionHistoryPanel.tsx",
  "components/DesignBriefPanel.tsx",
  "components/InspectReportPanel.tsx",
  "components/DesignAnalysis.tsx",
  "utils/manufacturingProfiles.ts",
  "utils/suggestions.ts",
];

const FORBIDDEN_SNIPPETS = [
  "????",
  "\u5de5\u7a0b\u8bbe\u8ba1\u7b80\u62a5",
  "\u9762\u5411 3D \u6253\u5370",
  "\u8bbe\u8ba1\u5047\u8bbe",
  "\u529f\u80fd\u9700\u6c42",
  "\u53ef\u6253\u5370\u6027\u76ee\u6807",
  "\u5f85\u786e\u8ba4\u95ee\u9898",
];

test("frontend Chinese copy has no mojibake placeholder remnants", () => {
  const failures: string[] = [];

  for (const file of FILES_TO_CHECK) {
    const text = readFileSync(join(FRONTEND_SRC, file), "utf8");
    for (const snippet of FORBIDDEN_SNIPPETS) {
      if (text.includes(snippet)) failures.push(`${file}: ${snippet}`);
    }
  }

  assert.deepEqual(failures, []);
});

test("design brief section titles use readable Chinese", () => {
  const text = readFileSync(join(FRONTEND_SRC, "components", "DesignBriefPanel.tsx"), "utf8");

  assert.equal(text.includes('title="\\u'), false);
  assert.match(text, /\\u5de5\\u7a0b\\u8bbe\\u8ba1\\u7b80\\u62a5/);
  assert.match(text, /\\u9762\\u5411 3D \\u6253\\u5370/);
  assert.match(text, /\\u8bbe\\u8ba1\\u5047\\u8bbe/);
  assert.match(text, /\\u529f\\u80fd\\u9700\\u6c42/);
  assert.match(text, /\\u53ef\\u6253\\u5370\\u6027\\u76ee\\u6807/);
  assert.match(text, /\\u5f85\\u786e\\u8ba4\\u95ee\\u9898/);
});
