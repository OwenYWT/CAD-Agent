import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/components/Viewer2D.tsx", import.meta.url),
  "utf8",
);

test("SVG artifacts render in an image context instead of executable DOM", () => {
  assert.doesNotMatch(source, /dangerouslySetInnerHTML/);
  assert.match(source, /new Blob/);
  assert.match(source, /type: "image\/svg\+xml"/);
  assert.match(source, /<img alt="二维工程图预览"/);
});

test("temporary SVG object URLs are revoked when the viewer unmounts", () => {
  assert.match(source, /URL\.revokeObjectURL\(objectUrl\)/);
  assert.match(source, /controller\.abort\(\)/);
});
