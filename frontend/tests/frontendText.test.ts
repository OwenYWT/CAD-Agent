import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND_SRC = join(import.meta.dirname, "..", "src");

const FILES_TO_CHECK = [
  "components/AgentRunTimeline.tsx",
  "components/RepairHistory.tsx",
  "components/VersionHistoryPanel.tsx",
  "components/changes/ChangeSetDialog.tsx",
  "components/validation/ValidationDialog.tsx",
  "components/DesignBriefPanel.tsx",
  "components/InspectReportPanel.tsx",
  "utils/manufacturingProfiles.ts",
  "utils/suggestions.ts",
];

const FORBIDDEN_SNIPPETS = [
  /\?{4,}/,
  /join\(" \? "\)/,
  / mm\?/ ,
  /queued: "\?"/,
  /running: "\?"/,
  /success: "\?"/,
  /failed: "\?"/,
  /skipped: "\?"/,
  /item\.ok \? "\?" : "\?"/,
  /v\{snapshot\.version\} \? \{snapshot\.source\}/,
  /className="text-gray-300">\?<\/span>/,
  /Design confirmation needed/,
  /Answer the open question before CAD generation continues\./,
  /View brief/,
  /AI \?{4} \+ DFM \?{2}/,
];

test("frontend Chinese copy has no mojibake placeholder remnants", () => {
  const failures: string[] = [];

  for (const file of FILES_TO_CHECK) {
    const text = readFileSync(join(FRONTEND_SRC, file), "utf8");
    for (const snippet of FORBIDDEN_SNIPPETS) {
      if (snippet.test(text)) failures.push(`${file}: ${snippet}`);
    }
  }

  assert.deepEqual(failures, []);
});
test("design brief section titles use readable Chinese", () => {
  const text = readFileSync(join(FRONTEND_SRC, "components", "DesignBriefPanel.tsx"), "utf8");

  assert.equal(text.includes("title=\"\\u"), false);
  assert.match(text, /\\u5de5\\u7a0b\\u8bbe\\u8ba1\\u7b80\\u62a5/);
  assert.match(text, /\\u9762\\u5411 3D \\u6253\\u5370/);
  assert.match(text, /\\u8bbe\\u8ba1\\u5047\\u8bbe/);
  assert.match(text, /\\u529f\\u80fd\\u9700\\u6c42/);
  assert.match(text, /\\u53ef\\u6253\\u5370\\u6027\\u76ee\\u6807/);
  assert.match(text, /\\u5f85\\u786e\\u8ba4\\u95ee\\u9898/);
});
test("history dialogs keep readable Chinese copy", () => {
  const changeSet = readFileSync(join(FRONTEND_SRC, "components/changes/ChangeSetDialog.tsx"), "utf8");
  const validation = readFileSync(join(FRONTEND_SRC, "components/validation/ValidationDialog.tsx"), "utf8");
  const history = readFileSync(join(FRONTEND_SRC, "components/VersionHistoryPanel.tsx"), "utf8");

  assert.match(changeSet, /变更审查/);
  assert.match(changeSet, /审查意见/);
  assert.match(changeSet, /没有父版本参数证据，或已记录参数值未变化。/);
  assert.match(validation, /设计简报等待确认/);
  assert.match(validation, /补充确认信息/);
  assert.match(validation, /工程检查与证据/);
  assert.match(history, /原生版本从 FCStd 恢复，源码版本重新执行已保存代码/);
  assert.match(history, /修改会进入持久任务与 Change Set 审查流程。/);
});

