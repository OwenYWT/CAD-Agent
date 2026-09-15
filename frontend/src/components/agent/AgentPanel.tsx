import { useMemo, useRef, useState } from "react";
import type { ConnectionState } from "../../hooks/useWebSocket";
import { useSessionStore } from "../../stores/sessionStore";
import type { EngineeringDomain } from "../../types/engineering";
import type { CloudDocument } from "../../types/document";
import type { ManufacturingProfile } from "../../types";
import type { RequirementBasis } from "../../types/requirements";
import { isContinuationOnly, taskState } from "../../adapters/taskState";
import { buildPromptSuggestions } from "../../utils/suggestions";
import { confirmDurableTask, EngineeringApiError } from "../../services/engineeringService";
import SuggestionPills from "../SuggestionPills";
import { Icon } from "../ui/Icon";
import { AGENT_CONTEXT_LABELS } from "./agentContext";
import RequirementCard, { RequirementSummary } from "./RequirementCard";
import TaskCard from "./TaskCard";

const ERROR_CODE_LABELS: Record<string, string> = {
  agent_plan_backend_mismatch: "建模计划与执行后端不匹配",
  invalid_edge_selection_mode: "边选择方式无效",
  topology_resolution_failed: "拓扑选择无法解析",
  topology_target_mismatch: "拓扑选择对象不匹配",
  sketch_malformed_constraint: "草图约束格式无效",
  sketch_redundant_constraints: "草图约束冗余",
  sketch_conflicting_constraints: "草图约束冲突",
  sketch_under_constrained: "草图尚未完全约束",
  sketch_solver_failed: "草图求解失败",
  parameter_state_stale: "参数状态已过期",
  parameter_state_missing: "缺少参数状态",
  parameter_duplicate_update: "参数修改存在重复项",
  parameter_not_editable: "参数不可编辑",
  parameter_value_type_invalid: "参数值类型无效",
  parameter_property_type_changed: "参数属性类型已变化",
  parameter_value_out_of_range: "参数值超出有效范围",
  subtractive_feature_no_effect: "减材特征没有移除材料",
  bom_input_missing: "BOM 输入缺失",
  bom_input_ambiguous: "BOM 输入不唯一",
  bom_input_integrity_failed: "BOM 输入完整性校验失败",
  bom_input_query_failed: "BOM 输入读取失败",
  bom_runtime_unsupported: "当前运行时不支持原生 BOM",
  bom_source_not_assembly: "BOM 来源不是有效装配体",
  bom_source_hierarchy_lost: "BOM 来源层级已丢失",
  bom_source_geometry_mismatch: "BOM 部件与装配几何不一致",
  bom_generation_failed: "BOM 生成失败",
  bom_empty: "BOM 没有部件行",
  bom_seal_failed: "BOM 产物封存失败",
  bom_seal_timed_out: "BOM 产物封存超时",
  sandbox_protocol_error: "隔离运行时响应无效",
  freecad_internal_error: "FreeCAD 内部错误",
};

function structuredErrorLabel(code?: string | null) {
  if (!code) return "任务执行失败";
  return ERROR_CODE_LABELS[code] || "本次任务失败";
}

interface AgentPanelProps {
  context: EngineeringDomain; connection: ConnectionState; suggestedPrompt?: string;
  onSend: (text:string,basis?:RequirementBasis)=>boolean;
  onCancel?:()=>void;onPreview?:()=>void;onCollapse?:()=>void;embedded?:boolean;
  selectionLabel?:string;onClearSelection?:()=>void;blockedReason?:string;
  onRetry?:()=>Promise<void>;onRecover?:(target:string)=>void;onReview?:()=>void;isAdmin?:boolean;canModify?:boolean;
  document?:CloudDocument | null;
}

export default function AgentPanel({context,connection,suggestedPrompt='',onSend,onCancel,onPreview,onCollapse,embedded=false,selectionLabel,onClearSelection,blockedReason,onRetry,onRecover,onReview,isAdmin=false,canModify=true,document}:AgentPanelProps) {
  const panel=useSessionStore(state=>state.getActivePanel());
  const task=taskState(panel,document);
  const [input,setInput]=useState(suggestedPrompt);const [pendingRequest,setPendingRequest]=useState<string|null>(null);
  const [error,setError]=useState<string|null>(null);const [confirmationPending,setConfirmationPending]=useState(false);
  const reviewedTarget=useRef<string|undefined>(undefined);
  const suggestions=useMemo(()=>buildPromptSuggestions({isEmpty:panel.messages.length===0,isGenerating:task.running,result:panel.result}),[panel.messages.length,panel.result,task.running]);
  const history=useMemo(()=>{
    const seen=new Set<string>();return panel.messages.filter(message=>{
      const id=message.result?.workflow_run_id;if(id){if(seen.has(id))return false;seen.add(id);}return true;
    });
  },[panel.messages]);
  const contextValue=task.snapshot?.request_payload.operation_context as {requirement_basis?:RequirementBasis}|undefined;
  const profile=task.snapshot?.request_payload.manufacturing_profile as ManufacturingProfile|null|undefined;
  const recover = () => {
    if (task.recovery === 'properties' || task.recovery === 'versions') onRecover?.(task.recovery);
    else {
      setInput(task.recoveryObjective);reviewedTarget.current=selectionLabel;
      setPendingRequest(task.continuationOnly && task.recoveryObjective ? task.recoveryObjective : null);
      setError(task.continuationOnly && !task.recoveryObjective ? '当前线程没有可恢复的具体需求，请补充建模目标。' : null);
    }
  };
  const reviewRequest=()=>{
    if(blockedReason){setError(blockedReason);return;}if(!input.trim())return;
    // A continuation after failure is a retry action, never a new objective consisting of “继续”.
    if(task.phase==='failed' && task.recovery!=='retry' && isContinuationOnly(input)){recover();return;}
    if(task.phase==='failed' && task.recovery==='retry' && isContinuationOnly(input) && onRetry){
      void onRetry().then(()=>setInput('')).catch(e=>setError(e instanceof Error?e.message:'重试失败'));return;
    }
    if(isContinuationOnly(input)){setError('请描述具体建模目标，或使用当前任务的确认、重试入口。');return;}
    reviewedTarget.current=selectionLabel;setPendingRequest(input.trim());setError(null);
  };
  const execute=(basis?:RequirementBasis)=>{
    if(!pendingRequest)return;
    if(reviewedTarget.current!==selectionLabel){setPendingRequest(null);setError('AI 目标或版本已改变，请重新预览修改范围。');return;}
    if(blockedReason){setError(blockedReason);return;}
    if(!onSend(pendingRequest,basis)){setError('实时连接尚未就绪，需求已保留。');return;}
    useSessionStore.getState().beginGeneration(undefined,basis);useSessionStore.getState().addMessage({role:'user',content:pendingRequest});
    setInput('');setPendingRequest(null);setError(null);
  };
  const submitServerConfirmation=async(accepted:boolean)=>{
    const confirmation=panel.durable?.confirmation;if(!confirmation || confirmationPending || task.status!=='waiting_confirmation')return;
    setConfirmationPending(true);setError(null);
    try{await confirmDurableTask(confirmation.workflow_run_id,accepted,accepted?'':'用户拒绝当前执行计划');}
    catch(cause){setError(cause instanceof EngineeringApiError && cause.code==='confirmation_not_waiting'?'任务状态已变化，正在等待最新状态。':cause instanceof Error?cause.message:'任务确认失败');}
    finally{setConfirmationPending(false);}
  };
  return <section className={`ww-agent-panel ${embedded?'ww-agent-panel--embedded':''}`}>
    {embedded?<header className="ww-pane-header"><div className="ww-agent-title"><span className="ww-agent-avatar">A</span><div><p className="ww-pane-eyebrow">工程协作</p><h2>Agent</h2></div></div>
      <div className="flex items-center gap-1"><span className={`ww-connection-dot ${connection==='connected'?'ww-connection-dot--online':''}`} title={connection==='connected'?'文档连接正常':'正在连接'}/>{onCollapse?<button aria-label="折叠 Agent" className="workspace-icon-button" onClick={onCollapse} type="button"><Icon name="minus" size={15}/></button>:null}</div></header>:null}
    <div className="ww-agent-thread">
      <div className="ww-agent-context"><strong>{AGENT_CONTEXT_LABELS[context]}</strong><span className="ml-auto">{connection==='connected'?'文档连接正常':'连接中'}</span></div>
      {task.objective && !pendingRequest?<RequirementSummary compact objective={task.objective} basis={contextValue?.requirement_basis || panel.requirementBasis} profile={profile}/>:null}
      {!pendingRequest?<TaskCard key={task.taskId || 'submitting'} task={task} onRetry={onRetry} onRecover={recover} onCancel={onCancel} onReview={onReview} isAdmin={isAdmin} canModify={canModify} failureLabel={structuredErrorLabel(task.errorCode)}/>:null}
      {pendingRequest && !task.hasSaved ? <RequirementCard key={pendingRequest} objective={pendingRequest} profile={profile} onBack={()=>setPendingRequest(null)} onConfirm={basis=>execute(basis)} disabled={task.running || !canModify}/>:null}
      {pendingRequest && task.hasSaved ? <div className="ww-agent-confirmation" data-task-phase="needs_input"><p>确认本次修改</p><p data-i18n-skip>{pendingRequest}</p><p>目标：{selectionLabel || '当前已保存版本'}。明确尺寸保持原值，遇到冲突需重新确认。</p><div className="mt-3 flex gap-2"><button className="workspace-button" onClick={()=>setPendingRequest(null)} type="button">返回修改</button><button className="workspace-button workspace-button--primary" onClick={()=>execute()} type="button">确认并执行</button></div></div>:null}
      {task.status==='waiting_confirmation' && panel.durable?.confirmation?<div className="ww-agent-confirmation ww-agent-confirmation--server" role="group" aria-label="服务端执行计划确认"><p>执行计划等待确认</p><dl><div><dt>需要确认</dt><dd>{panel.durable.confirmation.reason}</dd></div><div><dt>影响对象</dt><dd>{panel.durable.confirmation.affected_objects.map(item=>`${item.label}（${item.change}）`).join('、')}</dd></div></dl><details><summary>计划身份</summary><code>{panel.durable.confirmation.plan_hash}</code></details><div className="mt-3 flex gap-2"><button className="workspace-button" disabled={confirmationPending || !canModify} onClick={()=>void submitServerConfirmation(false)} type="button">拒绝</button><button className="workspace-button workspace-button--primary" disabled={confirmationPending || !canModify} onClick={()=>void submitServerConfirmation(true)} type="button">{confirmationPending?'提交中…':'确认并继续'}</button></div></div>:null}
      {(task.phase==='candidate' || task.phase==='saved') && panel.result?.success && onPreview?<button className="workspace-button my-2" onClick={onPreview} type="button"><Icon name="box" size={14}/>查看{task.phase==='candidate'?'候选模型':'已保存模型'}</button>:null}
      {history.length?<details className="ww-task-history"><summary>历史对话与尝试（{history.length}）</summary>{history.map((message,index)=><article className={`ww-agent-message ww-agent-message--${message.role==='user'?'user':'assistant'}`} key={`${message.role}:${index}`}><div className="ww-agent-message__meta">{message.role==='user'?'你':message.result?.needs_confirmation?'此前待确认':message.result?.success===false?'上次失败':'CAD Agent'}</div><div className="ww-agent-message__body">{message.content}</div>{message.result?.workflow_run_id?<small>任务 {message.result.workflow_run_id}</small>:null}</article>)}</details>:null}
      {error?<p className="ww-agent-error" role="alert">{error}</p>:null}
    </div>
    <div className="ww-agent-composer-wrap">
      {selectionLabel?<div className="mb-2 rounded border border-[var(--agent-border)] bg-[var(--agent-soft)] p-2 type-caption" data-testid="agent-selection">修改目标：{selectionLabel}{onClearSelection?<button className="ml-2 underline" onClick={onClearSelection} type="button">清除本次 AI 选择</button>:null}</div>:null}
      {blockedReason?<p role="status" className="mb-2 type-caption text-amber-800">{blockedReason}</p>:null}
      <details className="ww-task-history"><summary>需求示例</summary><SuggestionPills disabled={task.running} onSelect={suggestion=>setInput(suggestion.prompt)} suggestions={suggestions}/></details>
      <div className="ww-agent-composer"><textarea aria-label="询问 Agent" disabled={task.running || !canModify} onChange={e=>setInput(e.target.value)} onKeyDown={e=>{if((e.metaKey || e.ctrlKey) && e.key==='Enter')reviewRequest();}} placeholder="说明需求或对所选对象的修改…" rows={3} value={input}/>
        <div className="ww-agent-composer__toolbar"><span className="ww-agent-composer__shortcut">⌘ Enter 审查</span><button aria-label="审查请求" className="ww-agent-send" disabled={!input.trim() || task.running || !!blockedReason || !canModify} onClick={reviewRequest} type="button"><Icon name="send" size={15}/></button></div></div>
    </div>
  </section>;
}
