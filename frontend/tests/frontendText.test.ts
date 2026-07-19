import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND_SRC = join(import.meta.dirname, "..", "src");

const FILES_TO_CHECK = [
  "components/AgentRunTimeline.tsx",
  "components/RepairHistory.tsx",
  "components/ParameterPanel.tsx",
  "components/VersionHistoryPanel.tsx",
  "components/DesignBriefPanel.tsx",
  "components/DownloadPanel.tsx",
  "components/InspectReportPanel.tsx",
  "components/ChatPanel.tsx",
  "components/DesignAnalysis.tsx",
  "utils/manufacturingProfiles.ts",
  "utils/suggestions.ts",
];

const FORBIDDEN_SNIPPETS = [
  "????",
  'join(" ? ")',
  " mm?",
  'queued: "?"',
  'running: "?"',
  'success: "?"',
  'failed: "?"',
  'skipped: "?"',
  'item.ok ? "?" : "?"',
  'v{snapshot.version} ? {snapshot.source}',
  'className="text-gray-300">?</span>',
  "Design confirmation needed",
  "Answer the open question before CAD generation continues.",
  "View brief",
  "AI ???? + DFM ??",
  "???????????",
  "AI 璁捐",
  "姝ｅ湪鍒嗘瀽",
  "鎺ㄨ崘",
  "闅惧害",
  "閲嶆柊鍒嗘瀽",
  "闂娓呭崟",
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


test("design brief section titles do not render escaped unicode", () => {
  const text = readFileSync(join(FRONTEND_SRC, "components", "DesignBriefPanel.tsx"), "utf8");

  assert.equal(text.includes('title="\\u'), false);
  assert.match(text, /title=\{"设计假设"\}/);
  assert.match(text, /title=\{"可打印性目标"\}/);
  assert.match(text, /title=\{"验收标准"\}/);
  assert.match(text, /title=\{"待确认问题"\}/);
});
