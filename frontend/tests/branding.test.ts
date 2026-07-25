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
  assert.match(brand, /h-6 w-6/);
  assert.match(brand, /h-8 w-8/);

  assert.match(read("src/components/project/ProjectStart.tsx"), /<BrandMark/);
  const workspaceHeader = read("src/components/project/WorkspaceHeader.tsx");
  assert.match(workspaceHeader, /<BrandMark[^>]*showName=\{false\}/);
  assert.match(workspaceHeader, /aria-label=\{`WordsWave，返回项目流程：\$\{props\.project\.name\}`\}/);

  const loginPage = read("src/components/LoginPage.tsx");
  assert.match(loginPage, /<BrandMark/);
  assert.match(loginPage, /className="mb-6 w-full justify-center text-white"/);
  assert.match(loginPage, /nameAs="h1"/);

  const html = read("index.html");
  assert.match(html, /<title>WordsWave \| AI 工程工作台<\/title>/);
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
