import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = (path: string) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");

test("Q04 history restore submits the immutable revision ID, not the snapshot ID", () => {
  const workspace = source("components/workspace/EngineeringWorkspace.tsx");
  const restore = workspace.match(/const restoreSnapshot = \(snapshot: ModelSnapshotDetail\) => \{([\s\S]*?)\n {2}\};/)?.[1] || "";
  assert.match(restore, /restoreRevision\(snapshot\.revision_id \|\| snapshot\.id\)/);
  assert.match(restore, /snapshot\.files\?\.fcstd|snapshot\.files\.fcstd/);
  assert.match(restore, /executeWithProgress\(snapshot\.code\)/);
  assert.match(restore, /activePanelId !== panel\.id/);
  assert.doesNotMatch(restore, /snapshot\.panel_id/);
  const hook = source("hooks/useWebSocket.ts");
  assert.match(hook, /type: "restore_revision"/);
  assert.match(hook, /source_revision_id: revisionId/);
  assert.match(hook, /panel\?\.isGenerating|panel\.isGenerating/);
});

test("Q04 copy explains native file restore and stops parallel restore clicks", () => {
  const panel = source("components/VersionHistoryPanel.tsx");
  assert.match(panel, /FCStd/);
  assert.doesNotMatch(panel, /恢复会重新执行已保存代码/);
  assert.match(panel, /restoringId !== null/);
});
