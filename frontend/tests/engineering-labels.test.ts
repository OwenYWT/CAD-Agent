import assert from "node:assert/strict";
import test from "node:test";

import {
  engineeringCheckLabel,
  engineeringPromptLabel,
  engineeringSourceLabel,
  engineeringStatusLabel,
} from "../src/utils/engineeringLabels.ts";

test("known engineering protocol values are localized at the presentation boundary", () => {
  assert.equal(engineeringStatusLabel("pass"), "通过");
  assert.equal(engineeringSourceLabel("geometry_validator"), "几何验证器");
  assert.equal(engineeringSourceLabel("execute_code"), "参数化代码执行");
  assert.equal(engineeringCheckLabel("dimension_range"), "尺寸范围");
  assert.equal(engineeringPromptLabel("manual code execution"), "手动执行参数化建模代码");
});

test("unknown backend evidence remains visible instead of being invented", () => {
  assert.equal(engineeringSourceLabel("private_worker"), "private_worker");
  assert.equal(engineeringCheckLabel("custom_rule"), "custom_rule");
});
