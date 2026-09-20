import { useState } from "react";
import type { taskState } from "../../adapters/taskState";
import { engineeringTaskEventLabel } from "../../utils/engineeringLabels";
import { getTaskValidationEvidence, type TaskValidationEvidence } from "../../services/clients/tasks";

const STEP_NAMES:Record<string,string>={agent_requirements:'确认需求依据',agent_decompose:'拆解建模步骤',agent_plan:'制定执行计划',agent_freecad_operations:'构建原生特征',agent_geometry:'校验几何',agent_visual:'检查外观',agent_dfm:'检查制造条件',agent_seal:'准备候选版本'};
const STATUS:Record<string,string>={pending:'等待执行',ready:'等待执行',running:'正在执行',succeeded:'已完成',failed:'失败',cancelled:'已取消',timed_out:'超时',skipped:'未执行'};
const GATES: Record<string,string> = {geometry:'几何检查',visual:'外观检查',dfm:'制造条件检查',artifact_integrity:'文件完整性',bom:'物料清单检查'};

function ValidationEvidenceDetails({taskId,evidenceId}:{taskId:string;evidenceId:string}) {
  const [evidence,setEvidence]=useState<TaskValidationEvidence | null>(null);
  const [error,setError]=useState('');
  const [pending,setPending]=useState(false);
  const load=async()=>{
    if(pending)return;
    setPending(true);setError('');
    try{setEvidence(await getTaskValidationEvidence(taskId,evidenceId));}
    catch(cause){setError(cause instanceof Error ? cause.message : '检查证据读取失败');}
    finally{setPending(false);}
  };
  return <div>
    {!evidence ? <button className="workspace-button" type="button" disabled={pending} onClick={()=>void load()}>{pending?'正在核对检查证据…':'查看检查对象、版本与规则'}</button> : <div data-testid="validation-evidence">
      <p>{evidence.selected_for_revision ? `检查已绑定版本：${evidence.revision_id}` : '检查对象：执行中的中间产物，未作为最终版本的检查证据。'}</p>
      <p>对象清单：{evidence.staging_manifest_id}</p>
      {evidence.report.expected_dimensions_mm ? <p>检查采用的尺寸：{JSON.stringify(evidence.report.expected_dimensions_mm)} mm</p> : null}
      {Array.isArray(evidence.report.evaluated_rule_ids) ? <p>已执行规则：{evidence.report.evaluated_rule_ids.join('、') || '未提供'}</p> : null}
      {Array.isArray(evidence.report.unevaluated_rule_ids) && evidence.report.unevaluated_rule_ids.length ? <p>未验证规则：{evidence.report.unevaluated_rule_ids.join('、')}</p> : null}
      <p>这些检查不构成实物适配验证。</p>
      <details><summary>量测与规则原文</summary><pre className="whitespace-pre-wrap break-words">{JSON.stringify(evidence.report,null,2)}</pre></details>
      <details><summary>服务器已核对的证据哈希</summary><code>{evidence.evidence_hash}</code></details>
    </div>}
    {error?<p role="alert">{error}</p>:null}
  </div>;
}

export default function TaskCard({task,onRetry,onRecover,onCancel,onReview,isAdmin=false,canModify=true,failureLabel}:{
  task:ReturnType<typeof taskState>;onRetry?:()=>Promise<void>;onRecover?:()=>void;onCancel?:()=>void;onReview?:()=>void;isAdmin?:boolean;canModify?:boolean;failureLabel?:string;
}) {
  const [retrying,setRetrying]=useState(false);const [error,setError]=useState('');const [contact,setContact]=useState(false);const [copied,setCopied]=useState(false);
  const retry=async()=>{if(!onRetry || retrying)return;setRetrying(true);setError('');try{await onRetry();}catch(e){setError(e instanceof Error?e.message:'重试提交失败');}finally{setRetrying(false);}};
  const snapshot=task.snapshot;const planned=Array.isArray(snapshot?.agent?.plan?.steps)?snapshot.agent.plan.steps as Record<string,unknown>[]:[];
  const errorDetails = task.error?.details || {};
  const operationId = task.error && 'operation_id' in task.error ? task.error.operation_id : errorDetails.operation_id;
  const action = task.error && 'action' in task.error ? task.error.action : errorDetails.action;
  const copy=async()=>{try{await navigator.clipboard.writeText(`CAD 任务故障\n任务：${task.taskId}\n需求：${task.objective}\n错误：${task.errorCode}\n${task.errorMessage}`);setCopied(true);}catch{setError('无法访问剪贴板，请展开错误详情后手动复制。');}};
  return <section className={`ww-task-card ww-task-card--${task.phase}`} data-testid="authoritative-task" data-task-phase={task.phase} data-task-id={task.taskId || ''}>
    <header><span className="workspace-chip">{task.label}</span><strong role={task.phase==='failed'?'alert':'status'}>{task.phase==='failed' && !task.quota && task.status!=='cancelled' && failureLabel ? failureLabel : task.title}</strong>
      {task.running ? <span aria-label="任务正在执行" className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-[var(--agent)] border-t-transparent"/>:null}</header>
    {task.running ? <p className="type-caption">{snapshot?.agent?.current_step_kind ? STEP_NAMES[snapshot.agent.current_step_kind] || engineeringTaskEventLabel(snapshot.agent.current_step_kind) : '等待服务端执行进度'}{task.hasSaved ? '；已保存模型保持可查看。':''}</p>:null}
    {task.phase==='failed' ? <p className="type-caption">{task.hasSaved?'上一个有效版本保持不变。':'本次没有保存新模型。'}{task.continuationOnly ? '此任务只记录了“继续”或“重试”，缺少具体建模目标。请恢复历史需求并确认后再提交。' : '原需求已保留。'}{task.quota?'恢复模型服务额度后，可重试本次任务。':''}</p>:null}
    {task.phase==='candidate' ? <p className="type-caption">AI 待确认方案，尚未保存为当前版本。生成成功不代表适配验证通过。</p>:null}
    {task.phase==='needs_input' ? <p className="type-caption">{task.status==='unconfirmed'?'尚未收到明确的受理结果。请查询原请求状态，系统会沿用原请求标识，避免重复建模。':'等待必要信息或计划确认，此时没有执行建模。'}</p>:null}
    {planned.length ? <details className="ww-task-plan"><summary>本次计划（{planned.length} 步）</summary><ol>{planned.map((step,i)=><li key={String(step.step_key || i)}><details><summary>{String(step.description || '建模步骤')}</summary><dl><div><dt>操作</dt><dd>{String(step.kind || '未提供')}</dd></div><div><dt>任务需求</dt><dd>{task.objective}</dd></div><div><dt>影响对象</dt><dd>{Array.isArray(step.affected_object_ids)?step.affected_object_ids.join('、'):'未提供'}</dd></div></dl></details></li>)}</ol></details>:null}
    {snapshot?.steps?.length ? <details className="ww-task-steps"><summary>已记录步骤与结果（{snapshot.steps.length}）</summary><ol>{snapshot.steps.map(step=><li key={step.id}><details><summary>{STEP_NAMES[step.kind] || engineeringTaskEventLabel(step.kind)} · {task.phase==='failed' && ['pending','ready','running'].includes(step.status)?'已停止':STATUS[step.status] || '尚未完成'}</summary>
      <p>操作标识：{step.step_key}；执行尝试：{step.attempt_count}</p><p>任务需求：{task.objective}</p>{step.error_message?<p>{step.error_message}</p>:<p>结果：{STATUS[step.status] || '未提供'}</p>}</details></li>)}</ol></details>:null}
    {snapshot?.agent?.validations?.length ? <details><summary>检查结果（{snapshot.agent.validations.length}）</summary>{snapshot.agent.validations.map(check=><div key={check.evidence_id}><strong>{GATES[check.gate] || check.gate} · {check.outcome==='passed'?'通过':check.outcome==='failed'?'未通过':'未验证'}</strong><p>{check.mode==='required'?'必需检查':'参考检查'}；{check.issues.join('；') || '没有附加问题说明'}</p><ValidationEvidenceDetails taskId={snapshot.id} evidenceId={check.evidence_id}/></div>)}</details>:null}
    <div className="mt-3 flex flex-wrap gap-2">
      {task.running && onCancel ? <button className="workspace-button" type="button" disabled={!canModify || !task.taskId || task.status==='cancelling'} onClick={onCancel}>取消任务</button>:null}
      {task.phase==='candidate' && onReview ? <button className="workspace-button workspace-button--primary" type="button" onClick={onReview}>审阅候选与参数变化</button>:null}
      {task.phase==='failed' && task.recovery==='retry' && onRetry ? <button className="workspace-button workspace-button--primary" type="button" disabled={!canModify || retrying} onClick={()=>void retry()}>{retrying?'正在确认重试请求…':'重试本次任务'}</button>:null}
      {task.phase==='failed' && task.recovery!=='retry' && onRecover ? <button className="workspace-button workspace-button--primary" type="button" disabled={!canModify} onClick={onRecover}>{task.recovery==='properties'?'返回属性修正参数':task.recovery==='versions'?'返回版本重新确认':task.continuationOnly ? task.recoveryObjective ? '恢复历史需求并确认' : '补充建模目标' : '修改保留的需求'}</button>:null}
      {task.phase==='failed' ? <button className="workspace-button" type="button" onClick={()=>setContact(!contact)}>{isAdmin?'管理员处理说明':'联系管理员'}</button>:null}
    </div>
    {contact ? <div className="ww-agent-confirmation"><p>{isAdmin?'请核对模型服务额度与配置，恢复后重试保存的任务。':'请将故障摘要交给项目管理员，核对模型服务额度与配置。当前页面不会自动发送消息。'}</p><button className="workspace-button" type="button" onClick={()=>void copy()}>{copied?'故障摘要已复制':'复制故障摘要'}</button></div>:null}
    {task.phase==='failed' ? <details className="mt-2"><summary>错误码与调用详情</summary><dl><div><dt>任务</dt><dd>{task.taskId || '尚未受理'}</dd></div><div><dt>错误码</dt><dd>{task.errorCode || '未提供'}</dd></div>
      {typeof operationId === 'string' ? <div><dt>失败操作</dt><dd>{operationId}</dd></div> : null}
      {typeof action === 'string' ? <div><dt>建议动作</dt><dd>{action}</dd></div> : null}
      {typeof errorDetails.constraint_status === 'string' ? <div><dt>约束状态</dt><dd>{errorDetails.constraint_status}</dd></div> : null}
      {Object.entries(errorDetails).filter(([key]) => !['operation_id','action','constraint_status'].includes(key)).map(([key,value]) => <div key={key}><dt>{key}</dt><dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd></div>)}
      <div><dt>调用信息</dt><dd>{task.errorMessage || '无额外信息'}</dd></div></dl></details>:null}
    {error ? <p role="alert" className="type-caption text-red-700">{error}</p>:null}
  </section>;
}
