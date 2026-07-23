import assert from "node:assert/strict";
import test from "node:test";

import { DEFAULT_MANUFACTURING_PROFILE, MANUFACTURING_PROFILE_PRESETS, formatManufacturingProfile } from "../src/utils/manufacturingProfiles.ts";

test("provides FDM PLA as the default manufacturing profile", () => {
  assert.equal(DEFAULT_MANUFACTURING_PROFILE.process, "fdm");
  assert.equal(DEFAULT_MANUFACTURING_PROFILE.material, "PLA");
  assert.equal(DEFAULT_MANUFACTURING_PROFILE.nozzle_diameter_mm, 0.4);
  assert.equal(DEFAULT_MANUFACTURING_PROFILE.layer_height_mm, 0.2);
  assert.deepEqual(DEFAULT_MANUFACTURING_PROFILE.build_volume_mm, [220, 220, 250]);
});

test("formats manufacturing profile for user-visible context", () => {
  assert.equal(
    formatManufacturingProfile(DEFAULT_MANUFACTURING_PROFILE),
    "FDM PLA - \u55b7\u5634 0.4mm - \u5c42\u9ad8 0.2mm - 220x220x250mm",
  );
  assert.equal(MANUFACTURING_PROFILE_PRESETS.some((preset) => preset.id === "fdm-petg"), true);
});
