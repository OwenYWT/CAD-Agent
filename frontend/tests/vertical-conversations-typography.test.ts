import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";

const FRONTEND = join(import.meta.dirname, "..");
const SOURCE = join(FRONTEND, "src");
const read = (path: string) => readFileSync(join(FRONTEND, path), "utf8");

const sourceFiles = (extension: RegExp) => readdirSync(SOURCE, { recursive: true, withFileTypes: true })
  .filter((entry) => entry.isFile() && extension.test(entry.name))
  .map((entry) => join(entry.parentPath, entry.name));

test("engineering conversations render in the existing sidebar as a vertical navigation", () => {
  const panelTabs = read("src/components/PanelTabs.tsx");
  const sidebar = read("src/components/project/ProjectSidebar.tsx");

  assert.match(panelTabs, /useI18n/);
  assert.match(panelTabs, /aria-label=\{translate\("工程线程"\)\}/);
  assert.match(panelTabs, /ww-conversation-list/);
  assert.match(panelTabs, /aria-current=\{isActive \? "page" : undefined\}/);
  assert.doesNotMatch(panelTabs, /overflow-x-auto/);
  assert.doesNotMatch(panelTabs, /scrollIntoView/);
  assert.match(
    sidebar,
    /className="ww-sidebar-section"[\s\S]*?<p className="ww-sidebar-heading">工程线程<\/p>[\s\S]*?<PanelTabs \/>/,
  );

  const createIndex = panelTabs.indexOf("ww-conversation-create");
  const listIndex = panelTabs.indexOf("ww-conversation-list");
  assert.ok(createIndex >= 0 && listIndex > createIndex, "new-conversation action must precede rows");
});

test("conversation rows scroll vertically without moving the sidebar", () => {
  const css = read("src/index.css");
  const rule = css.match(/\.ww-conversation-list\s*\{([\s\S]*?)\}/)?.[1] ?? "";

  assert.match(rule, /flex-direction:\s*column/);
  assert.match(rule, /max-height:\s*min\(240px,\s*32vh\)/);
  assert.match(rule, /overflow-y:\s*auto/);
  assert.match(rule, /overflow-x:\s*hidden/);
});

test("conversation labels use existing Chinese and English resources", () => {
  const panelTabs = read("src/components/PanelTabs.tsx");
  const i18n = read("src/i18n/I18nProvider.tsx");

  assert.match(i18n, /\["工程线程", "Engineering threads"\]/);
  assert.match(i18n, /\["新建对话", "New conversation"\]/);
  assert.match(panelTabs, /translate\("工程线程"\)/);
  assert.match(panelTabs, /translate\("新建对话"\)/);
});

test("default conversation titles follow the active locale without mutating storage", () => {
  const tabs = read("src/components/PanelTabs.tsx");

  assert.match(tabs, /translate\(panel\.title\)/);
  assert.doesNotMatch(tabs, /data-i18n-skip>\{panel\.title\}/);
});

test("production TSX uses semantic typography utilities", () => {
  const excludedCanvasFile = join(SOURCE, "components", "ModelAnnotations.tsx");
  const forbiddenClass = /(?:\btext-(?:xs|sm|base|lg|xl|2xl|3xl)\b|text-\[(?:\d|clamp\(|length:)[^\]]*\]|\bleading-(?:none|tight|snug|normal|relaxed|loose|\d+)\b|leading-\[[^\]]+\]|\bfont-(?:thin|extralight|light|normal|medium|semibold|bold|extrabold|black)\b|font-\[[^\]]+\])/g;
  const forbiddenInlineStyle = /\b(?:fontSize|fontWeight|lineHeight)\s*:/g;

  for (const file of sourceFiles(/\.tsx$/)) {
    if (file === excludedCanvasFile) continue;
    const source = readFileSync(file, "utf8");
    const classMatches = [...source.matchAll(forbiddenClass)].map((match) => match[0]);
    const styleMatches = [...source.matchAll(forbiddenInlineStyle)].map((match) => match[0]);
    assert.deepEqual(
      [...classMatches, ...styleMatches],
      [],
      `${relative(FRONTEND, file)} contains ad hoc typography`,
    );
  }
});

test("production CSS uses only approved typography tokens", () => {
  for (const file of sourceFiles(/\.css$/)) {
    const source = readFileSync(file, "utf8");
    assert.doesNotMatch(source, /(^|[;{]\s*)font\s*:/m, `${relative(FRONTEND, file)} uses font shorthand`);

    for (const match of source.matchAll(/(font-size|line-height|font-weight)\s*:\s*([^;]+);/g)) {
      const [, property, value] = match;
      const allowed = property === "font-weight"
        ? /^var\(--type-(?:caption|body|control|section|page|display)-weight\)$/
        : property === "font-size"
          ? /^var\(--type-(?:caption|body|section|page|display)-size\)$/
          : /^var\(--type-(?:caption|body|section|page|display)-line\)$/;
      assert.match(
        value.trim(),
        allowed,
        `${relative(FRONTEND, file)} has unsupported ${property}: ${value.trim()}`,
      );
    }
  }
});
