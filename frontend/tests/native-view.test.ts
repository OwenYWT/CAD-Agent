import assert from "node:assert/strict";
import test from "node:test";
import { engineeringSourceLabel, projectViewedDocument, viewIdentity, viewLabel } from "../src/adapters/documentView.ts";
import { comparisonStaleness } from "../src/adapters/branchView.ts";
import type { BranchComparison, DocumentBranches } from "../src/types/branches.ts";
import { adaptArtifacts, adaptEngineeringProject } from "../src/adapters/projectAdapter.ts";
import { useDraftGuardStore } from "../src/stores/draftGuard.ts";
import type { CloudDocument, DocumentRevisionView } from "../src/types/document.ts";
import type { GenerationResult } from "../src/types/index.ts";
import type { PanelState } from "../src/stores/sessionStore.ts";

const documentId = "a1bfc5f0-5c47-453f-b05c-2e6a71e85930";
const artifactId = "dcb15cc6-59dd-4783-bcb9-be274953ba86";
const head = {document_id: documentId, head_revision_id:"head", revision_id:"head", state_version:9,
  can_edit:true, can_share:true, can_export:true, features:[{id:"f",label:"新标注"}]} as CloudDocument;
const evidence = {document_id:documentId,revision_id:"old",features:[],head_state_version:7} as unknown as DocumentRevisionView;

test("history has its immutable revision and never borrows the current generation label or edit permission", () => {
  const view = viewIdentity(head, {mode:"history",revisionId:"old"});
  assert.doesNotMatch(viewLabel(view), /v9/);
  assert.equal(projectViewedDocument(head,view,null,true), null);
  const projected = projectViewedDocument(head,view,evidence,true)!;
  assert.equal(projected.revision_id,"old");
  assert.equal(projected.head_revision_id,"head");
  assert.equal(projected.can_edit,false);
  assert.equal(projectViewedDocument(head,view,{...evidence,document_id:"foreign"},true),null);
  assert.equal(projectViewedDocument(head,view,{...evidence,revision_id:"other"},true),null);
});

test("a committed view follows a new head and keeps live annotations and generation after ABA", () => {
  const view = viewIdentity(head,{mode:"committed",revisionId:"old"});
  assert.equal(view.viewedRevisionId,"head");
  const projected = projectViewedDocument(head,view,{...evidence,revision_id:"head"},true)!;
  assert.deepEqual(projected.features,head.features);
  assert.equal(projected.state_version,9);
  assert.equal(projectViewedDocument(head,view,null,false)?.can_edit,false);
});

test("authenticated revision artifacts remain downloadable and current committed models are complete", () => {
  const result = {success:true,request_id:"w",files:{step:`/api/documents/${documentId}/artifacts/${artifactId}`}} as GenerationResult;
  const artifacts = adaptArtifacts(result);
  assert.equal(artifacts.length,1);
  assert.equal(artifacts[0].name,"model.step");
  const model = adaptEngineeringProject("s",{id:"p",messages:[],result} as unknown as PanelState,null,{...head,view_mode:"committed"});
  assert.equal(model.project.status,"completed");
  assert.equal(model.stages.find(s=>s.id==="mechanical")?.status,"completed");
  const viewer = adaptEngineeringProject("s",{id:"p",messages:[],result} as unknown as PanelState,null,{...head,can_export:false,view_mode:"committed"});
  assert.equal(viewer.exports.length,0);
  assert.equal(viewer.artifacts.length,1);
  assert.equal(viewer.stages.find(s=>s.id==="release")?.available,false);
  assert.equal(adaptArtifacts({...result,files:{step:"https://external.test/model.step"}}).length,0);
});

test("branch comparison expires on either side's ABA generation and on missing source evidence", () => {
  const comparison = {document_id:documentId,target_revision_id:"head",target_state_version:9,
    source_document_id:"branch",source_revision_id:"source",source_state_version:4} as BranchComparison;
  const branches = {branches:[{document_id:"branch",head_revision_id:"source",state_version:4}]} as DocumentBranches;
  assert.deepEqual(comparisonStaleness(comparison,head,branches),{target:false,source:false,sourceKnown:true});
  assert.equal(comparisonStaleness(comparison,{...head,state_version:11},branches).target,true);
  assert.equal(comparisonStaleness(comparison,head,{...branches,branches:[{...branches.branches[0],state_version:6}]}).source,true);
  assert.deepEqual(comparisonStaleness(comparison,head,null),{target:false,source:true,sourceKnown:false});
});

test("engineering reports identify the viewed revision without verifying a different model", () => {
  assert.equal(engineeringSourceLabel("head","head","head"),"本次查看的已提交版本");
  assert.equal(engineeringSourceLabel("old","old","head"),"本次查看的历史版本");
  assert.equal(engineeringSourceLabel("old","head","head"),"其他修订，不能验证当前查看模型");
  assert.equal(engineeringSourceLabel("head","candidate","head"),"其他修订，不能验证当前查看模型");
});

test("draft guard keeps values until explicit discard, then switches once", () => {
  const store = useDraftGuardStore.getState();
  let discarded = 0, switched = 0;
  store.register("draft",{label:"参数",discard:()=>{discarded++;}});
  assert.equal(store.request(()=>{switched++;}),false);
  store.stay();
  assert.equal(discarded,0); assert.equal(switched,0);
  store.request(()=>{switched++;}); store.discardAndContinue();
  assert.equal(discarded,1); assert.equal(switched,1);
  assert.deepEqual(useDraftGuardStore.getState().drafts,{});
});
