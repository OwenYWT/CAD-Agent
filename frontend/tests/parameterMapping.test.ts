import assert from "node:assert/strict";
import test from "node:test";

import { splitEngineeringParameters } from "../src/utils/parameterMapping.ts";
import type { CADParameter, DesignBrief } from "../src/types/index.ts";

test("puts parameters matching critical dimensions first", () => {
  const parameters: CADParameter[] = [
    {
      name: "wall_thickness_mm",
      display_name: "Wall Thickness",
      value: 2.4,
      default_value: 2.4,
      type: "number",
      min: 0.8,
      max: 8,
      step: 0.2,
      unit: "mm",
      group: "Print",
      comment: "Wall thickness",
      line: 1,
    },
    {
      name: "slot_width_mm",
      display_name: "Slot Width",
      value: 16,
      default_value: 16,
      type: "number",
      min: 6,
      max: 30,
      step: 0.5,
      unit: "mm",
      group: "Dimensions",
      comment: "Cable slot width",
      line: 2,
    },
  ];
  const brief: DesignBrief = {
    intent_summary: "Cable clip",
    artifact_type: "clip",
    manufacturing_posture: "printable",
    assumptions: [],
    critical_dimensions: [
      { name: "slot width", value: 16, unit: "mm", reason: "Must fit the cable." },
    ],
    functional_requirements: [],
    printability_targets: [],
    acceptance_criteria: [],
    open_questions: [],
  };

  const result = splitEngineeringParameters(parameters, brief);

  assert.deepEqual(result.critical.map((item) => item.parameter.name), ["slot_width_mm"]);
  assert.equal(result.critical[0].dimension?.reason, "Must fit the cable.");
  assert.deepEqual(result.standard.map((item) => item.name), ["wall_thickness_mm"]);
});

test("keeps all parameters standard when no design brief exists", () => {
  const parameters: CADParameter[] = [
    {
      name: "height_mm",
      display_name: "Height",
      value: 40,
      default_value: 40,
      type: "number",
      line: 1,
    },
  ];

  const result = splitEngineeringParameters(parameters, null);

  assert.deepEqual(result.critical, []);
  assert.deepEqual(result.standard.map((item) => item.name), ["height_mm"]);
});

