import assert from 'node:assert/strict';
import test from 'node:test';
import { reduceDraft } from '../src/adapters/draftHistory.ts';
import { engineeringCheckResponse } from '../src/adapters/engineeringCheck.ts';
import { adaptValidation } from '../src/adapters/projectAdapter.ts';
import {visibleFeatureRows} from '../src/adapters/featureTree.ts';
import type {SemanticFeature} from '../src/types/document.ts';

const workflow='11111111-2222-4333-8444-555555555555';
test('collapsing a nested model branch preserves its siblings and search can locate hidden children',()=>{
  const rows=[['root',1],['child',2],['grandchild',3],['sibling',2],['other',1]].map(([id,level])=>({feature:{id,label:id,kernel_name:id} as SemanticFeature,level:Number(level)}));
  assert.deepEqual(visibleFeatureRows(rows,new Set(['child']),'').map(r=>r.feature.id),['root','child','sibling','other']);
  assert.deepEqual(visibleFeatureRows(rows,new Set(['root']),'').map(r=>r.feature.id),['root','other']);
  assert.deepEqual(visibleFeatureRows(rows,new Set(['root']),'grandchild').map(r=>r.feature.id),['grandchild']);
});
test('pending and legacy timeout check receipts retain the original workflow without claiming analysis',()=>{
  for (const [status,body,header] of [[202,{workflow_run_id:workflow},null],[504,{},workflow]] as const) {
    assert.deepEqual(engineeringCheckResponse(status,body,header),{kind:'pending',workflowId:workflow});
  }
  assert.throws(()=>engineeringCheckResponse(202,{}));
  assert.throws(()=>engineeringCheckResponse(202,{workflow_run_id:workflow},'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'));
  assert.throws(()=>engineeringCheckResponse(200,{status:'pending',workflow_run_id:workflow}));
  assert.throws(()=>engineeringCheckResponse(503,{}));
});
test('unmeasured or indeterminate check outcomes do not produce pass or geometry repair failures',()=>{
  const analysis={design_score:null,design_summary:'必要测量未完成',dfm_issues:[],rule_violations:[],evaluation_status:'indeterminate',analysis_errors:['壁厚未测量']};
  const completed=engineeringCheckResponse(200,analysis);
  assert.equal(completed.kind,'complete');
  const checks=adaptValidation({success:true,revision_id:'r',validation:{gates:[
    {gate:'dfm',outcome:'indeterminate',mode:'required'},{gate:'visual',outcome:'disabled',mode:'advisory'}]}} as Parameters<typeof adaptValidation>[0],analysis as Parameters<typeof adaptValidation>[1]);
  assert.equal(checks.length,3);assert.ok(checks.every(check=>check.status==='unknown'));
  assert.equal(checks[0].outcome,'indeterminate');assert.equal(checks[1].outcome,'disabled');
});
test('draft undo redo is scoped and clears redo after an edit or new saved baseline',()=>{
  let state={past:[] as number[],present:10,future:[] as number[]};
  state=reduceDraft(state,{kind:'set',value:20});state=reduceDraft(state,{kind:'set',value:30});
  state=reduceDraft(state,{kind:'undo'});assert.equal(state.present,20);
  state=reduceDraft(state,{kind:'redo'});assert.equal(state.present,30);
  state=reduceDraft(state,{kind:'undo'});state=reduceDraft(state,{kind:'set',value:25});
  assert.equal(reduceDraft(state,{kind:'redo'}).present,25);
  state=reduceDraft(state,{kind:'reset',value:40});assert.equal(reduceDraft(state,{kind:'undo'}).present,40);
  const unchanged=reduceDraft(state,{kind:'set',value:40});assert.equal(unchanged,state);
  for (let i=0;i<70;i++) state=reduceDraft(state,{kind:'set',value:i});
  assert.equal(state.past.length,50);
});
