import { useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ConnectionState } from "../../hooks/useWebSocket";
import { useSessionStore } from "../../stores/sessionStore";
import type { EngineeringDomain } from "../../types/engineering";
import type { CloudDocument } from "../../types/document";
import type { ManufacturingProfile } from "../../types";
import type { RequirementBasis } from "../../types/requirements";
import { isContinuationOnly, taskState } from "../../adapters/taskState";
import { buildPromptSuggestions } from "../../utils/suggestions";
import { confirmDurableTask } from "../../services/clients/tasks";
import { EngineeringApiError } from "../../services/clients/http";
import SuggestionPills from "../SuggestionPills";
import { Icon } from "../ui/Icon";
import { AGENT_CONTEXT_LABELS } from "./agentContext";
import RequirementCard, { RequirementSummary } from "./RequirementCard";
import TaskCard from "./TaskCard";
import ChatComposer from "./ChatComposer";

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
  mainThread?: boolean;
  modelArtifact?: ReactNode;
  viewError?: string;
  onReviewDraft?: (note: string) => void;
  context: EngineeringDomain; connection: ConnectionState; suggestedPrompt?: string;
  onSend: (text:string,basis?:RequirementBasis)=>boolean;
  onCancel?:()=>void;onPreview?:()=>void;onCollapse?:()=>void;embedded?:boolean;
  selectionLabel?:string;onClearSelection?:()=>void;blockedReason?:string;
  onRetry?:()=>Promise<void>;onRecover?:(target:string,featureId?:string)=>void;onReview?:()=>void;isAdmin?:boolean;canModify?:boolean;canConfirm?:boolean;
  document?:CloudDocument | null;
}

export default function AgentPanel({mainThread=false,modelArtifact,viewError,onReviewDraft,context,connection,suggestedPrompt='',onSend,onCancel,onPreview,onCollapse,embedded=false,selectionLabel,onClearSelection,blockedReason,onRetry,onRecover,onReview,isAdmin=false,canModify=true,canConfirm=canModify,document}:AgentPanelProps) {
  const panel=useSessionStore(state=>state.getActivePanel());
  const task=taskState(panel,document);
  const [input,setInput]=useState(suggestedPrompt);const [pendingRequest,setPendingRequest]=useState<string|null>(null);
  const [error,setError]=useState<string|null>(null);const [confirmationPending,setConfirmationPending]=useState(false);
  const [reviseRequirements,setReviseRequirements]=useState(false);
  const reviewedTarget=useRef<string|undefined>(undefined);
  const suggestions=useMemo(()=>buildPromptSuggestions({isEmpty:panel.messages.length===0,isGenerating:task.running,result:panel.result}),[panel.messages.length,panel.result,task.running]);
  const lastUserIndex = panel.messages.findLastIndex(message => message.role === 'user');
  const hasCurrentRequest = lastUserIndex >= 0 && panel.messages[lastUserIndex].content === task.objective;
  const thread = useRef<HTMLDivElement>(null);
  const stream = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const composer = useRef<HTMLTextAreaElement>(null);
  const examples = useRef<HTMLDetailsElement>(null);
  const history = useMemo(() => {
    const seen = new Set<string>();
    return panel.messages.flatMap((message, index) => {
      const id = message.result?.workflow_run_id;
      if (id) {
        const key = JSON.stringify([id, message.content]);
        if (seen.has(key) || (id === task.taskId && !pendingRequest && message.result?.task_status !== 'waiting_confirmation'
          && (message.content === 'CAD 模型已生成' || message.content === '任务已取消' || message.content.startsWith('生成失败:')))) return [];
        seen.add(key);
      }
      return [{message, index}];
    });
  }, [panel.messages, task.taskId, pendingRequest]);
  useLayoutEffect(() => {
    const viewport = thread.current, content = stream.current;
    if (!viewport || !content) return;
    const stick = () => { if (followLatest.current) viewport.scrollTop = viewport.scrollHeight; };
    stick();
    const observer = new ResizeObserver(stick);
    observer.observe(content);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);
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
    if(!input.trim() || task.running || pendingRequest)return;
    followLatest.current = true;
    if (task.phase === 'candidate' && onReviewDraft) { onReviewDraft(input.trim()); return; }
    if(blockedReason){setError(blockedReason);return;}
    // A continuation after failure is a retry action, never a new objective consisting of “继续”.
    if(task.phase==='failed' && task.recovery!=='retry' && isContinuationOnly(input)){recover();return;}
    if(task.phase==='failed' && task.recovery==='retry' && isContinuationOnly(input) && onRetry){
      void onRetry().then(()=>setInput('')).catch(e=>setError(e instanceof Error?e.message:'重试失败'));return;
    }
    if(isContinuationOnly(input)){setError('请描述具体建模目标，或使用当前任务的确认、重试入口。');return;}
    reviewedTarget.current=selectionLabel;setPendingRequest(input.trim());setReviseRequirements(false);setError(null);
  };
  const execute=(basis?:RequirementBasis)=>{
    if(!pendingRequest)return;
    if(reviewedTarget.current!==selectionLabel){setPendingRequest(null);setError('AI 目标或版本已改变，请重新预览修改范围。');return;}
    if(blockedReason){setError(blockedReason);return;}
    const confirmedRequest = basis?.target || pendingRequest;
    if(!onSend(confirmedRequest,basis)){setError('实时连接尚未就绪，需求已保留。');return;}
    useSessionStore.getState().beginGeneration(undefined,basis);useSessionStore.getState().addMessage({role:'user',content:confirmedRequest});
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
    {embedded && !mainThread?<header className="ww-pane-header"><div className="ww-agent-title"><Icon name="message" size={16}/><h2>Agent</h2></div>
      {onCollapse?<button aria-label="折叠 Agent" className="workspace-icon-button" onClick={onCollapse} type="button"><Icon name="minus" size={15}/></button>:null}</header>:null}
    <div className="ww-agent-thread" ref={thread} onScroll={event => {
      const node = event.currentTarget;
      followLatest.current = node.scrollHeight - node.clientHeight - node.scrollTop < 72;
    }}>
      <div className="ww-chat-stream" ref={stream} onClickCapture={event => {
        if (event.target instanceof Element && event.target.closest('summary')) followLatest.current = false;
      }}>
      {context !== 'mechanical' ? <p className="type-caption text-[var(--muted)]">{AGENT_CONTEXT_LABELS[context]}</p> : null}
      {connection !== 'connected' ? <p role="status" className="type-caption text-amber-800">{connection === 'disconnected' ? '实时连接已断开' : '实时连接中'}</p> : null}
      {history.map(({message, index}) => index === lastUserIndex && hasCurrentRequest
        ? <RequirementSummary key={index} compact objective={task.objective} basis={contextValue?.requirement_basis || panel.requirementBasis} profile={profile}/>
        : message.role === 'user'
          ? <div key={index} className="ww-request-message" aria-label="用户需求"><p data-i18n-skip>{message.content}</p></div>
          : <article key={index} className="ww-chat-reply" aria-label="CAD-Agent"><p data-i18n-skip={message.result ? undefined : true}>{message.content}</p>
              {message.result?.workflow_run_id ? <details><summary>任务详情</summary>
                {message.result.needs_confirmation ? <span>此前待确认 </span> : message.result.success === false && message.result.task_status !== 'cancelled' ? <span>上次失败 </span> : null}
                <code>{message.result.workflow_run_id}</code></details> : null}</article>)}
      {task.objective && !hasCurrentRequest ? <RequirementSummary compact objective={task.objective} basis={contextValue?.requirement_basis || panel.requirementBasis} profile={profile}/> : null}
      {!pendingRequest ? <TaskCard key={task.taskId || 'submitting'} task={task} onRetry={onRetry} onRecover={recover} onTakeover={id=>onRecover?.("properties",id)} onCancel={onCancel} composerHasStop={mainThread} onReview={onReview} isAdmin={isAdmin} canModify={canModify} failureLabel={structuredErrorLabel(task.errorCode)}>{modelArtifact}</TaskCard> : modelArtifact}
      {viewError ? <p role="alert" className="ww-agent-error">{viewError}</p> : null}
      {pendingRequest ? <div className="ww-request-message" aria-label="待确认的需求"><p data-i18n-skip>{pendingRequest}</p></div> : null}
      {pendingRequest && (!task.hasSaved || reviseRequirements) ? <RequirementCard key={pendingRequest} objectiveInThread objective={pendingRequest} profile={profile} initialBasis={reviseRequirements ? contextValue?.requirement_basis || panel.requirementBasis || undefined : undefined} onBack={()=>{setPendingRequest(null);setReviseRequirements(false);}} onConfirm={basis=>execute(basis)} disabled={task.running || !canModify}/>:null}
      {pendingRequest && task.hasSaved && !reviseRequirements ? <div className="ww-agent-confirmation" data-task-phase="needs_input"><p>确认本次修改</p><p>目标：{selectionLabel || '当前已保存版本'}。修改明确尺寸或依据时，请先重新确认需求；原版本记录保留。</p><div className="mt-3 flex flex-wrap gap-2"><button className="workspace-button" onClick={()=>setPendingRequest(null)} type="button">返回修改</button><button className="workspace-button" onClick={()=>setReviseRequirements(true)} type="button">修改需求尺寸或依据</button><button className="workspace-button workspace-button--primary" onClick={()=>execute()} type="button">确认并执行</button></div></div>:null}
      {task.status==='waiting_confirmation' && panel.durable?.confirmation?<div className="ww-agent-confirmation ww-agent-confirmation--server" role="group" aria-label="服务端执行计划确认"><p>执行计划等待确认</p><dl><div><dt>需要确认</dt><dd>{panel.durable.confirmation.reason}</dd></div><div><dt>影响对象</dt><dd>{panel.durable.confirmation.affected_objects.map(item=>`${item.label}（${item.change}）`).join('、')}</dd></div></dl><details><summary>计划身份</summary><code>{panel.durable.confirmation.plan_hash}</code></details><div className="mt-3 flex gap-2"><button className="workspace-button" disabled={confirmationPending || !canConfirm} onClick={()=>void submitServerConfirmation(false)} type="button">拒绝</button><button className="workspace-button workspace-button--primary" disabled={confirmationPending || !canConfirm} onClick={()=>void submitServerConfirmation(true)} type="button">{confirmationPending?'提交中…':'确认并继续'}</button></div></div>:null}
      {!modelArtifact && (task.phase==='candidate' || task.phase==='saved') && panel.result?.success && onPreview?<button className="workspace-button my-2" onClick={onPreview} type="button"><Icon name="box" size={14}/>查看{task.phase==='candidate'?'候选模型':'已保存模型'}</button>:null}
      {error?<p className="ww-agent-error" role="alert">{error}</p>:null}
      </div>
    </div>
    <footer className="ww-chat-dock">
      {selectionLabel ? <div className="ww-chat-selection" data-testid="agent-selection">已选中：<span data-i18n-skip>{selectionLabel}</span>
        {onClearSelection ? <button type="button" onClick={onClearSelection}>清除本次 AI 选择</button> : null}</div> : null}
      {blockedReason && !onReviewDraft ? <p role="status" className="ww-chat-notice">{blockedReason}</p> : null}
      <ChatComposer inputRef={composer} label="询问 Agent" submitLabel="审查请求" value={input} onChange={setInput} onSubmit={reviewRequest}
        placeholder={pendingRequest ? "请先确认上方的建模条件" : onReviewDraft ? "描述你希望怎样修改这个候选模型…" : "继续描述你的设计…"}
        disabled={!!pendingRequest || (!canModify && !onReviewDraft)}
        sendDisabled={task.running || (!!blockedReason && !onReviewDraft)}
        onStop={task.running && mainThread ? onCancel : undefined}
        stopDisabled={!canModify || !task.taskId || task.status === 'cancelling'}
        tools={canModify && suggestions.length > 0 && !pendingRequest ? <details className="ww-chat-suggestions" ref={examples}
          onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) event.currentTarget.open = false; }}
          onKeyDown={event => {
            if (event.key !== "Escape" || !event.currentTarget.open) return;
            event.preventDefault();
            event.currentTarget.open = false;
            event.currentTarget.querySelector("summary")?.focus();
          }}>
          <summary aria-label="需求示例" title="需求示例"><Icon name="plus" size={19}/></summary>
          <div><SuggestionPills onSelect={suggestion => {
            setInput(suggestion.prompt);
            if (examples.current) examples.current.open = false;
            composer.current?.focus();
          }} suggestions={suggestions}/></div>
        </details> : null} />
    </footer>
  </section>;
}
