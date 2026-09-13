import assert from "node:assert/strict";
import test from "node:test";
import { adaptEngineeringProject } from "../src/adapters/projectAdapter.ts";
import { adaptDurableChangeSet, buildChangeSet } from "../src/adapters/changeSetAdapter.ts";
import type { PanelState } from "../src/stores/sessionStore.ts";
import type { CloudDocument } from "../src/types/document.ts";
import type { DurableChangeSetDetail } from "../src/types/engineering.ts";
import type { ModelSnapshotDetail } from "../src/types/index.ts";
import type { EngineeringTaskSummary } from "../src/types/engineeringTask.ts";

const gates = [{gate:"geometry", mode:"required", outcome:"passed"},
  {gate:"visual", mode:"advisory", outcome:"failed"}];
const panel = { id:"p", messages:[{role:"user",content:"four corner holes"}],
  result:{success:true, request_id:"request", files:{step:"/api/files/request/model.step"}, validation:{is_watertight:true,status:"passed",issue_count:0,gates}},
  isGenerating:false, lastError:null } as PanelState;

test("failed design check makes the project and manufacturing stage show an issue", () => {
  const model = adaptEngineeringProject("session", panel);
  assert.equal(model.project.status, "issue");
  assert.equal(model.stages.find(s => s.id === "manufacturing")?.status, "issue");
});

test("legacy watertight result cannot override failed design evidence", () => {
  const snapshot = {id:"r", panel_id:"p", prompt:"four holes", result:panel.result} as ModelSnapshotDetail;
  assert.equal(buildChangeSet(snapshot, null).validation.status, "fail");
});

test("legacy durable zero-issue summary is checked against its actual gates", () => {
  const detail = {id:"c", validation_summary:{status:"passed",issue_count:0,gates},
    risk_summary:{},change_summary:{},audit_log:[],base_artifacts:[],candidate_artifacts:[],
    base_manifest:{},candidate_manifest:{}} as unknown as DurableChangeSetDetail;
  const change = adaptDurableChangeSet(detail, "p");
  assert.equal(change.validation.status, "fail");
  assert.doesNotMatch(change.validation.summary, /问题 0 项/);
});

test("native document enables simulation and only its current revision results complete the stage", () => {
  const document = {document_id:"d",head_revision_id:"r2",revision_id:"r2",fcstd:{sha256:"source"}} as CloudDocument;
  const tasks = [{task_kind:"linear_static",source_revision_id:"r1",status:"succeeded"}] as EngineeringTaskSummary[];
  let model = adaptEngineeringProject("session", panel, null, document, tasks);
  assert.equal(model.stages.find(s=>s.id==="simulation")?.available, true);
  assert.equal(model.stages.find(s=>s.id==="simulation")?.status, "not_started");
  model = adaptEngineeringProject("session", panel, null, document, [{...tasks[0],source_revision_id:"r2"}]);
  assert.equal(model.stages.find(s=>s.id==="simulation")?.status, "completed");
  model = adaptEngineeringProject("session", panel, null, document, [{...tasks[0],source_revision_id:"r2",status:"failed"}]);
  assert.equal(model.stages.find(s=>s.id==="simulation")?.status, "issue");
  model = adaptEngineeringProject("session", panel, null, {...document,revision_id:"r1",view_mode:"history"}, tasks);
  assert.equal(model.stages.find(s=>s.id==="simulation")?.status, "completed");
});
