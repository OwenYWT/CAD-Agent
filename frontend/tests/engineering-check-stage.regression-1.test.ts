import assert from "node:assert/strict";
import test from "node:test";

import { adaptEngineeringProject } from "../src/adapters/projectAdapter.ts";
import type { PanelState } from "../src/stores/sessionStore.ts";
import type { DesignAnalysis } from "../src/types/index.ts";

// Regression: ISSUE-002 — a completed real engineering check left the workflow stage at "未开始"
// Found by /qa on 2026-08-25
// Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-08-25.md

const panel: PanelState = {
  id: "panel-1",
  title: "工程任务",
  messages: [{ role: "user", content: "创建立方体" }],
  currentStep: null,
  result: {
    success: true,
    request_id: "request-1",
    code: "result = box(20)",
    files: {
      step: "/api/files/request-1/model.step",
      stl: "/api/files/request-1/model.stl",
    },
  },
  isGenerating: false,
  stepHistory: [],
  generationStartTime: null,
  baselineVersion: 0,
  multiStepProgress: null,
  lastError: null,
  activeRun: null,
  artifactUpdates: [],
};

const analysis: DesignAnalysis = {
  design_score: 82,
  design_summary: "检查完成",
  structural_issues: [],
  functional_notes: [],
  recommended_process: "FDM",
  process_compatibility: {},
  dfm_issues: [{
    category: "wall_thickness",
    severity: "warning",
    description: "壁厚需要确认",
    suggestion: "复核制造参数",
    location: "当前模型",
  }],
  estimated_difficulty: "low",
  geometry: {},
  rule_violations: [],
};

test("real analysis evidence completes the manufacturing-check stage", () => {
  const model = adaptEngineeringProject("session-1", panel, analysis);
  const manufacturing = model.stages.find((stage) => stage.id === "manufacturing");

  assert.equal(model.validation.length, 1);
  assert.equal(manufacturing?.status, "completed");
  assert.equal(manufacturing?.summary, "已有 1 项真实检查结果。");
});
