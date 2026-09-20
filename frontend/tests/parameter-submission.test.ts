import assert from 'node:assert/strict';
import test from 'node:test';
import { parameterSubmissionState } from '../src/adapters/parameterSubmission.ts';
import type { CloudDocument, DocumentOperation } from '../src/types/document.ts';
const doc = {head_revision_id:'next',revision_id:'next',view_mode:'committed'} as CloudDocument;
const op = {id:'mine',status:'committed',result_revision_id:'next'} as DocumentOperation;
test('only this request and its synchronized saved head unlock parameter editing',()=>{
  assert.equal(parameterSubmissionState('mine',[op],doc).saved,true);
  assert.equal(parameterSubmissionState('other',[op],doc).saved,false);
  assert.equal(parameterSubmissionState('mine',[{...op,status:'reviewable'}],doc).saved,false);
  assert.equal(parameterSubmissionState('mine',[op],{...doc,head_revision_id:'old'}).saved,false);
  assert.equal(parameterSubmissionState('mine',[op],{...doc,view_mode:'candidate'}).saved,false);
});
test('failed and rejected attempts preserve a correction path without treating them as saved',()=>{
  for (const status of ['failed','cancelled','rejected','rolled_back']) {
    const state=parameterSubmissionState('mine',[{...op,status}],doc);
    assert.equal(state.failed,true);assert.equal(state.saved,false);
  }
  assert.equal(parameterSubmissionState('mine',[],doc).failed,false);
});
