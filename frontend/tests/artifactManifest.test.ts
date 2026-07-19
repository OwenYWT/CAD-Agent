import assert from "node:assert/strict";
import test from "node:test";

import { buildArtifactManifest, manifestFilename } from "../src/utils/artifactManifest.ts";
import type { GenerationResult } from "../src/types/index.ts";

test("builds a traceable CAD artifact manifest", () => {
  const result: GenerationResult = {
    success: true,
    request_id: "req-123",
    snapshot_id: "snap-1",
    version: 4,
    manufacturing_profile: {
      process: "fdm",
      material: "PETG",
      nozzle_diameter_mm: 0.4,
      layer_height_mm: 0.2,
      build_volume_mm: [220, 220, 250],
    },
    files: { stl: "/outputs/model.stl", step: "/outputs/model.step" },
    code: "import cadquery as cq\nresult = cq.Workplane('XY').box(10, 20, 3)",
    parameters: [
      {
        name: "width_mm",
        display_name: "Width",
        value: 20,
        default_value: 20,
        type: "number",
        unit: "mm",
        line: 2,
      },
    ],
    design_brief: {
      intent_summary: "Printable plate",
      artifact_type: "plate",
      manufacturing_posture: "printable",
      assumptions: [],
      critical_dimensions: [],
      functional_requirements: [],
      printability_targets: [],
      acceptance_criteria: [],
      open_questions: [],
    },
    validation: {
      is_watertight: true,
      bounding_box: null,
      volume: 600,
    },
    repair_history: [],
    recovery_actions: [
      {
        label: "Retry simpler",
        prompt: "Regenerate with simpler geometry.",
        reason: "Execution failed.",
        action_type: "retry_simpler",
      },
    ],
  };

  const manifest = buildArtifactManifest(result, {
    prompt: "make a printable plate",
    generatedAt: "2026-07-19T12:00:00.000Z",
  });

  assert.equal(manifest.source, "CAD-Agent");
  assert.equal(manifest.adaptation_source, "forgecad-public-kit engineering artifact package");
  assert.equal(manifest.request_id, "req-123");
  assert.equal(manifest.snapshot_id, "snap-1");
  assert.equal(manifest.version, 4);
  assert.equal(manifest.prompt, "make a printable plate");
  assert.deepEqual(manifest.reference_attachments, []);
  assert.equal(manifest.manufacturing_profile?.material, "PETG");
  assert.deepEqual(manifest.available_files, [
    { format: "stl", url: "/outputs/model.stl" },
    { format: "step", url: "/outputs/model.step" },
  ]);
  assert.equal(manifest.code?.includes("cadquery"), true);
  assert.equal(manifest.parameters?.[0].name, "width_mm");
  assert.equal(manifest.design_brief?.intent_summary, "Printable plate");
  assert.equal(manifest.validation?.volume, 600);
  assert.equal(manifest.recovery_actions?.[0].action_type, "retry_simpler");
});

test("uses request id in manifest filename when available", () => {
  assert.equal(manifestFilename("req-123"), "cad-agent-manifest-req-123.json");
  assert.equal(manifestFilename(null), "cad-agent-manifest-latest.json");
});
test("extracts reference attachments into manifest", () => {
  const result: GenerationResult = {
    success: true,
    request_id: "req-ref",
    files: { stl: "/outputs/ref.stl" },
    validation: { is_watertight: true, bounding_box: null, volume: 100 },
  };

  const manifest = buildArtifactManifest(result, {
    prompt:
      "make a holder\n\n" +
      "Reference attachments:\n" +
      "Reference attachment: sketch.png, image/png, 248 KB, category=image\n" +
      "Reference attachment: fit.step, application/octet-stream, 4 KB, category=cad\n" +
      "User intent: use these attachments as shape, proportion, or fit references.",
    generatedAt: "2026-07-19T12:00:00.000Z",
  });

  assert.deepEqual(manifest.reference_attachments, [
    {
      name: "sketch.png",
      mime_type: "image/png",
      size_bytes: 253952,
      size_label: "248 KB",
      category: "image",
    },
    {
      name: "fit.step",
      mime_type: "application/octet-stream",
      size_bytes: 4096,
      size_label: "4 KB",
      category: "cad",
    },
  ]);
});

