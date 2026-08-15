import assert from "node:assert/strict";
import test from "node:test";

import { adaptArtifacts, adaptEngineeringProject } from "../src/adapters/projectAdapter.ts";
import type { PanelState } from "../src/stores/sessionStore.ts";


function panelWithResult(result: PanelState["result"]): PanelState {
  return {
    id: "panel-real-flow",
    title: "工程任务",
    messages: [{ role: "user", content: "创建一个支架" }],
    currentStep: null,
    result,
    isGenerating: false,
    stepHistory: [],
    generationStartTime: null,
    baselineVersion: 0,
    multiStepProgress: null,
    lastError: null,
  };
}


test("frontend does not expose release success without backend artifact URLs", () => {
  const model = adaptEngineeringProject(
    "session-1",
    panelWithResult({
      request_id: "request-no-files",
      success: true,
      files: {},
      code: "result = box(1, 1, 1)",
    }),
  );

  assert.equal(model.artifacts.length, 0);
  assert.equal(model.stages.find((stage) => stage.id === "mechanical")?.status, "issue");
  assert.equal(model.stages.find((stage) => stage.id === "release")?.available, false);
});


test("artifact adapter rejects placeholder paths", () => {
  const artifacts = adaptArtifacts({
    request_id: "request-1",
    success: true,
    files: {
      step: "placeholder.step",
      stl: "/api/files/request-1/result.stl",
    },
  });

  assert.deepEqual(artifacts.map((artifact) => artifact.format), ["STL"]);
});
