import assert from "node:assert/strict";
import test from "node:test";

import type { Parameter } from "../src/types/engineering.ts";
import { parameterValueError } from "../src/utils/parameterValidation.ts";

const parameter: Parameter = {
  id: "width",
  label: "宽度",
  group: "基础尺寸",
  value: 20,
  defaultValue: 20,
  unit: "mm",
  min: 1,
  max: 100,
};

test("parameter validation rejects missing and out-of-range values", () => {
  assert.equal(parameterValueError(parameter, Number.NaN), "请输入有效数字");
  assert.equal(parameterValueError(parameter, 0), "不能小于 1mm");
  assert.equal(parameterValueError(parameter, 101), "不能大于 100mm");
});

test("parameter validation accepts a finite value inside backend limits", () => {
  assert.equal(parameterValueError(parameter, 24), null);
});
