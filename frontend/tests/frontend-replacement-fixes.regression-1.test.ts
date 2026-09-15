import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

// Regression: FR-001..FR-006 — remaining legacy surfaces and missing reference behavior
// Found by /qa on 2026-08-27
// Report: .gstack/qa-reports/frontend-replacement-20260827T092737-c3a7/qa-report.md

const FRONTEND = join(import.meta.dirname, "..");
const read = (path: string) => readFileSync(join(FRONTEND, path), "utf8");

test("reported legacy surfaces use the reference workspace token system", () => {
  const files = [
    "src/App.tsx",
    "src/components/LoginPage.tsx",
    "src/components/ErrorBoundary.tsx",
    "src/components/SettingsDrawer.tsx",
    "src/components/DFMRuleConfig.tsx",
    "src/components/KnowledgeGraph.tsx",
    "src/components/CapabilityCatalog.tsx",
    "src/components/CapabilityRunner.tsx",
    "src/components/AccountPanel.tsx",
    "src/components/VersionHistoryPanel.tsx",
  ];

  for (const file of files) {
    const source = read(file);
    assert.doesNotMatch(source, /(?:bg|text|border|ring)-(?:slate|gray|sky|indigo)-\d+/, file);
  }
});

test("production components do not retain the legacy cool-gray palette", () => {
  const sourceRoot = join(FRONTEND, "src");
  const files = readdirSync(sourceRoot, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && /\.(?:ts|tsx)$/.test(entry.name))
    .map((entry) => join(entry.parentPath, entry.name));

  for (const file of files) {
    assert.doesNotMatch(
      readFileSync(file, "utf8"),
      /(?:bg|text|border|ring|from|to)-(?:slate|gray|zinc|neutral|stone|sky|blue|indigo|violet)-\d+/,
      file,
    );
  }
});

test("account popover closes on Escape and restores focus", () => {
  const account = read("src/components/AccountPanel.tsx");

  assert.match(account, /useEffect/);
  assert.match(account, /event\.key === "Escape"/);
  assert.match(account, /triggerRef/);
  assert.match(account, /panelRef/);
  assert.match(account, /triggerRef\.current\?\.focus\(\)/);
});

test("responsive workspace keeps one right pane and a mounted mobile model", () => {
  const css = read("src/index.css");
  const shell = read("src/components/workspace/WorkspaceShell.tsx");
  assert.match(css, /grid-template-columns: minmax\(0, 1fr\) 5px var\(--ww-right-width\)/);
  assert.doesNotMatch(css, /var\(--ww-inspector-width\)/);
  assert.match(css, /\.ww-primary-pane \{ display:block; height:100%; \}/);
  assert.match(shell, /hidden=\{drawerOpen\}/);
  assert.match(shell, /hidden=\{!drawerOpen\}/);
});

test("production UI exposes a persisted React language switch", () => {
  const main = read("src/main.tsx");
  const i18n = read("src/i18n/I18nProvider.tsx");
  const header = read("src/components/project/WorkspaceHeader.tsx");
  const runner = read("src/components/CapabilityRunner.tsx");

  assert.match(main, /I18nProvider/);
  assert.match(i18n, /cad-agent-locale/);
  assert.match(i18n, /localStorage\.setItem/);
  assert.match(i18n, /document\.documentElement\.lang/);
  assert.match(i18n, /\["补充约束", "Add constraints"\]/);
  assert.match(i18n, /\^会话 \(\.\+\)\$/);
  assert.match(i18n, /\["CAD 任务执行失败", "CAD task failed"\]/);
  assert.match(i18n, /\["物料清单", "Bill of materials"\]/);
  assert.match(i18n, /\["模型存在非闭合边界。", "The model has non-watertight boundaries\."\]/);
  assert.match(header, /LanguageSwitch/);
  assert.match(header, /translate\(props\.project\.branch\)/);
  assert.match(runner, /aria-label=\{translate\("Action 参数 JSON"\)\}/);
});

test("version timestamps follow the selected application locale", () => {
  const versions = read("src/components/VersionHistoryPanel.tsx");

  assert.match(versions, /useI18n/);
  assert.match(versions, /locale === "zh" \? "zh-CN" : "en-US"/);
  assert.doesNotMatch(versions, /toLocaleString\(\)/);
  assert.match(versions, /translate\(engineeringSourceLabel/);
  assert.match(versions, /translate\(engineeringStatusLabel/);
});

test("QA harness mounts the production workspace instead of maintaining a parallel UI", () => {
  const harness = read("qa/engineering-workspace.tsx");

  assert.match(harness, /EngineeringWorkspace/);
  assert.match(harness, /I18nProvider/);
  assert.doesNotMatch(harness, /ProjectStart|ProjectFlow|MechanicalWorkspace/);
});

test("dynamic translations process every mutation batch and change-set rows have stable unique keys", () => {
  const i18n = read("src/i18n/I18nProvider.tsx");
  const changeSet = read("src/components/changes/ChangeSetDialog.tsx");

  assert.doesNotMatch(i18n, /let applying = false|requestAnimationFrame\(\(\) => \{ applying/);
  assert.match(changeSet, /changeSet\.files\.map\(\(file, index\)/);
  assert.match(changeSet, /file\.format}:\$\{file\.kind}:\$\{index}/);
});
