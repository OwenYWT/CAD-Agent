import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const FRONTEND = join(import.meta.dirname, "..");
const read = (path: string) => readFileSync(join(FRONTEND, path), "utf8");

test("the empty project screen stays inside the reference application shell", () => {
  const workspace = read("src/components/workspace/EngineeringWorkspace.tsx");
  const projectStart = read("src/components/project/ProjectStart.tsx");

  assert.doesNotMatch(
    workspace,
    /if \(!hasProject\)[\s\S]{0,160}return <>[\s\S]{0,160}<ProjectStart/,
  );
  assert.match(workspace, /if \(!hasProject\)[\s\S]{0,500}ww-app-shell/);
  assert.match(projectStart, /embedded\?: boolean/);
});
