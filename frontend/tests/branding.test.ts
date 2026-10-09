import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND = join(import.meta.dirname, "..");
const read = (path: string) => readFileSync(join(FRONTEND, path), "utf8");

test("WordsWave branding is centralized and covers every product surface", () => {
  assert.equal(existsSync(join(FRONTEND, "src/assets/wordswave-logo.jpg")), true);

  const brand = read("src/components/common/BrandMark.tsx");
  assert.match(brand, /wordswave-logo\.jpg/);
  assert.match(brand, /WordsWave/);
  assert.match(brand, /object-contain/);
  assert.match(brand, /onError/);
  assert.match(brand, /setFailed\(true\)/);
  assert.match(brand, /aria-label=\{showName \? undefined : "WordsWave"\}/);
  assert.match(brand, /h-7 w-7/);
  assert.match(brand, /h-9 w-9/);
  assert.match(brand, />W<\/span>/);

  assert.match(read("src/components/project/ProjectStart.tsx"), /<BrandMark className="type-section-heading"/);
  const projectSidebar = read("src/components/project/ProjectSidebar.tsx");
  assert.match(projectSidebar, /<BrandMark[^>]*showName=\{!collapsed\}/);
  const workspaceHeader = read("src/components/project/WorkspaceHeader.tsx");
  assert.match(workspaceHeader, /aria-label=\{translate\(`返回项目流程：\$\{props\.project\.name\}`\)\}/);
  assert.match(workspaceHeader, /data-i18n-skip>\{props\.project\.name\}/);

  const loginPage = read("src/components/LoginPage.tsx");
  assert.match(loginPage, /<BrandMark/);
  assert.match(loginPage, /className="ww-login-brand"/);
  assert.match(loginPage, /<LanguageSwitch className="workspace-button ww-auth-language"/);
  assert.match(loginPage, /description="参数化建模工作台"/);
  assert.match(loginPage, /nameAs="h1"/);
  assert.match(loginPage, /nameClassName="type-display-heading"/);

  const html = read("index.html");
  assert.match(html, /<title>WordsWave<\/title>/);
  assert.equal(html.includes("AI 工程工作台"), false);
  assert.match(html, /rel="icon" type="image\/jpeg" href="\/src\/assets\/wordswave-logo\.jpg"/);
});

test("live frontend surfaces no longer render the old product name", () => {
  for (const path of [
    "index.html",
    "src/components/project/ProjectStart.tsx",
    "src/components/project/WorkspaceHeader.tsx",
    "src/components/LoginPage.tsx",
  ]) {
    assert.equal(read(path).includes("CAD Agent"), false, path);
  }
});


test("removed explanatory copy stays absent from live surfaces", () => {
  const liveSurfaceText = [
    read("index.html"),
    read("src/components/project/ProjectStart.tsx"),
    read("src/components/project/ProjectFlow.tsx"),
  ].join("\n");

  for (const removed of [
    "AI 工程工作台",
    "描述硬件需求，系统会逐步生成设计、参数、验证和可导出的工程产物。",
    "默认只展示项目阶段和下一步。工程模型、参数、检查和文件会在进入对应工作区后展开。",
  ]) {
    assert.equal(liveSurfaceText.includes(removed), false, removed);
  }
});

test("workspace header keeps icon-only mobile actions accessible", () => {
  const workspaceHeader = read("src/components/project/WorkspaceHeader.tsx");

  assert.match(workspaceHeader, /key: "checks", label: "检查"/);
  assert.match(workspaceHeader, /aria-label=\{item.label\}/);
  assert.match(workspaceHeader, /aria-expanded=\{menuOpen\}/);
  assert.match(workspaceHeader, /aria-label="导出"/);
});
