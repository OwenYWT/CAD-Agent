import assert from "node:assert/strict";
import test from "node:test";
import { useSessionStore } from "../src/stores/sessionStore.ts";
import type { DurableTaskSnapshot } from "../src/types/index.ts";
import { taskState } from "../src/adapters/taskState.ts";

function panel() { useSessionStore.getState().reset(); return useSessionStore.getState().getActivePanel(); }
function snapshot(id: string, status: string): DurableTaskSnapshot {
  return { id, project_id: 'project', kind: 'mcad.agent_v2', status,
    request_payload: {branch_id:'branch',objective:'制作手机壳'}, last_event_sequence:8,
    error_code:status==='failed'?'ProviderQuotaError':null, error_message:status==='failed'?'模型服务额度不足':null };
}

test('terminal snapshot cannot be restarted by replayed running steps', () => {
  const p=panel();const s=useSessionStore.getState();s.applyDurableSnapshot(snapshot('task-1','failed'),p.id);
  s.setStep({step:'planning',message:'需求与方案进行中',status:'running',detail:{source:'durable_task_event',sequence:2,workflow_run_id:'task-1'}},p.id);
  assert.equal(s.getActivePanel().isGenerating,false);
  assert.equal(s.getActivePanel().currentStep,null);
});

test('waiting for confirmation has no execution spinner', () => {
  const p=panel();const s=useSessionStore.getState();s.applyDurableSnapshot(snapshot('task-1','waiting_confirmation'),p.id);
  assert.equal(s.getActivePanel().isGenerating,false);
});

test('old workflow snapshot and events cannot overwrite a new retry', () => {
  const p=panel();const s=useSessionStore.getState();s.applyDurableSnapshot(snapshot('task-1','failed'),p.id);
  s.beginGeneration();s.setDurableWorkflowStarted({workflow_run_id:'task-2',project_id:'project',branch_id:'branch',expected_base_revision_id:'base',panel_id:p.id,status:'pending'});
  s.applyDurableSnapshot(snapshot('task-1','failed'),p.id);
  s.applyDurableEvent({id:'old',workflow_run_id:'task-1',sequence:50,event_type:'workflow.failed',payload:{},occurred_at:new Date().toISOString()},p.id);
  assert.equal(s.getActivePanel().durable?.workflowRunId,'task-2');
  assert.equal(s.getActivePanel().lastError,null);
  assert.equal(s.getActivePanel().isGenerating,true);
});

test('failed modification keeps the last valid model and clears current activity', () => {
  const p=panel();const s=useSessionStore.getState();s.setResult({success:true,workflow_run_id:'good',files:{fcstd:'/api/files/good/model.FCStd'},code:'native'},p.id);
  s.beginGeneration();s.setDurableWorkflowStarted({workflow_run_id:'bad',project_id:'project',branch_id:'branch',expected_base_revision_id:'base',panel_id:p.id,status:'pending'});
  s.applyDurableSnapshot(snapshot('bad','failed'),p.id);
  assert.equal(s.getActivePanel().result?.files?.fcstd,'/api/files/good/model.FCStd');
  assert.equal(s.getActivePanel().currentStep,null);
});

test('the five phases separate task outcome from saving and document sync', () => {
  const p=panel(); const s=useSessionStore.getState();
  assert.equal(taskState(s.getActivePanel()).phase,'needs_input');
  s.beginGeneration();
  assert.equal(taskState(s.getActivePanel()).phase,'running');
  s.setDurableWorkflowStarted({workflow_run_id:'one',project_id:'project',branch_id:'branch',expected_base_revision_id:'base',panel_id:p.id,status:'pending'});
  s.applyDurableSnapshot(snapshot('one','failed'),p.id);
  assert.equal(taskState(s.getActivePanel()).phase,'failed');
  assert.equal(taskState(s.getActivePanel()).quota,true);
  s.beginGeneration();
  assert.equal(taskState(s.getActivePanel()).phase,'running');
  assert.equal(taskState(s.getActivePanel()).quota,false);
  s.setDurableWorkflowStarted({workflow_run_id:'two',project_id:'project',branch_id:'branch',expected_base_revision_id:'base',panel_id:p.id,status:'pending'});
  const candidate = {...snapshot('two','succeeded'), change_set:{id:'candidate',status:'proposed',candidate_revision_id:'rev',base_revision_id:'base'}} as DurableTaskSnapshot;
  s.applyDurableSnapshot(candidate,p.id);
  assert.equal(taskState(s.getActivePanel()).phase,'candidate');
  s.applyDurableSnapshot({...candidate,change_set:{...candidate.change_set!,status:'committed'}},p.id);
  assert.equal(taskState(s.getActivePanel()).phase,'saved');
});

test('cancelled tasks stop, with an explicit title and no confirmation spinner', () => {
  const p=panel();const s=useSessionStore.getState();
  s.applyDurableSnapshot(snapshot('one','cancelled'),p.id);
  assert.equal(taskState(s.getActivePanel()).running,false);
  assert.equal(taskState(s.getActivePanel()).title,'任务已取消');
});

test('reviewing an older candidate cannot overwrite a newer running task', () => {
  const p=panel();const s=useSessionStore.getState();
  s.applyDurableSnapshot(snapshot('new','running'),p.id);
  s.applyDurableChangeSet({id:'old-candidate',source_workflow_run_id:'old',status:'committed',workflow_status:'succeeded'} as Parameters<typeof s.applyDurableChangeSet>[0],p.id);
  assert.equal(s.getActivePanel().durable?.workflowRunId,'new');
  assert.equal(taskState(s.getActivePanel()).phase,'running');
});

test('a rejected submission stops before acceptance without attributing the error to the previous task', () => {
  const p = panel(); const s = useSessionStore.getState();
  s.applyDurableSnapshot(snapshot('previous', 'failed'), p.id);
  s.beginGeneration();
  s.addMessage({role: 'user', content: '将孔径改为 7 mm'});
  s.setResult({success: false, submission_outcome: 'rejected', submission_id: 'request',
    error: {type: 'StaleBaseRevision', message: '版本已更新，请重新确认'}}, p.id);
  const current = taskState(s.getActivePanel());
  assert.equal(s.getActivePanel().submissionPending, false);
  assert.equal(current.phase, 'failed');
  assert.equal(current.taskId, null);
  assert.equal(current.errorCode, 'StaleBaseRevision');
  assert.equal(current.objective, '将孔径改为 7 mm');
  assert.equal(current.quota, false);
  s.applyDurableSnapshot(snapshot('previous', 'failed'), p.id);
  assert.equal(taskState(s.getActivePanel()).errorCode, 'StaleBaseRevision');
  s.beginGeneration();
  assert.equal(taskState(s.getActivePanel()).phase, 'running');
  assert.equal(taskState(s.getActivePanel()).errorMessage, '');
});

test('terminal workflow status supersedes an earlier prepared confirmation', () => {
  for (const status of ['cancelled', 'failed', 'succeeded']) {
    const p = panel(); const s = useSessionStore.getState();
    s.applyDurableSnapshot({...snapshot('legacy', 'running'), last_event_sequence: 0}, p.id);
    s.applyDurableEvent({id: 'prepared', workflow_run_id: 'legacy', sequence: 1,
      event_type: 'source.prepared', payload: {needs_confirmation: true}, occurred_at: new Date().toISOString()}, p.id);
    s.applyDurableSnapshot({...snapshot('legacy', status), change_set: status === 'succeeded'
      ? {id: 'candidate', status: 'pending_review'} : undefined} as DurableTaskSnapshot, p.id);
    assert.equal(s.getActivePanel().result?.needs_confirmation, false);
    assert.equal(taskState(s.getActivePanel()).phase, status === 'succeeded' ? 'candidate' : 'failed');
  }
});

test('an uncertain submission shows the recovery state without reusing an older quota failure', () => {
  const p=panel(); const s=useSessionStore.getState();
  s.applyDurableSnapshot(snapshot('old','failed'), p.id);
  s.beginGeneration(); s.setError('任务是否受理尚未确认',p.id);
  const current=taskState(s.getActivePanel());
  assert.equal(current.phase,'needs_input');
  assert.equal(current.title,'等待确认任务是否受理');
  assert.equal(current.taskId,null);
  assert.equal(current.quota,false);
  s.setDurableWorkflowStarted({workflow_run_id:'new',project_id:'project',branch_id:'branch',expected_base_revision_id:'base',panel_id:p.id,status:'running'});
  assert.equal(taskState(s.getActivePanel()).phase,'running');
});

test('parameter and restore failures return to their own operation entry rather than replaying expired leases', () => {
  const p=panel(); const s=useSessionStore.getState();
  for (const [field,recovery] of [['structured_modification','properties'],['revision_restore','versions']]) {
    s.applyDurableSnapshot({...snapshot('one','failed'),kind:'mcad.agent.v2.modify',
      request_payload:{...snapshot('one','failed').request_payload,[field]:{}}},p.id);
    assert.equal(taskState(s.getActivePanel()).recovery,recovery);
  }
});
