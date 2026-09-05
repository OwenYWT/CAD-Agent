import { useEffect, useMemo, useRef, useState } from "react";
import type { ConnectionState } from "../../hooks/useWebSocket";
import { useSessionStore } from "../../stores/sessionStore";
import type { EngineeringDomain } from "../../types/engineering";
import { engineeringTaskEventLabel } from "../../utils/engineeringLabels";
import { buildPromptSuggestions } from "../../utils/suggestions";
import { useI18n } from "../../i18n/I18nContext";
import {
  confirmDurableTask,
  EngineeringApiError,
} from "../../services/engineeringService";
import SuggestionPills from "../SuggestionPills";
import { Icon } from "../ui/Icon";
import { AGENT_CONTEXT_LABELS } from "./agentContext";

const RUN_STATUS_LABELS: Record<string, string> = {
  running: "运行中",
  succeeded: "已成功",
  failed: "已失败",
  blocked: "待处理",
  cancelled: "已取消",
  observed: "已观测",
};

const STEP_LABELS: Record<string, string> = {
  plan_design: "需求规划",
  generate_cad_code: "生成 CAD 代码",
  executing_code: "执行建模",
  execute_code: "执行代码",
  execute_cad_code: "执行 CAD 代码",
  repair_code: "自动修复代码",
  resume_available: "可继续任务",
  finalize_result: "整理结果",
};

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
  return ERROR_CODE_LABELS[code] || code;
}

function shortId(value?: string | null) {
  return value ? value.slice(0, 8) : "-";
}

function runStatusLabel(status?: string | null) {
  if (!status) return RUN_STATUS_LABELS.observed;
  return RUN_STATUS_LABELS[status] || status;
}

function stepLabel(step: string) {
  return STEP_LABELS[step] || engineeringTaskEventLabel(step);
}

function taskText(value?: string | null) {
  return engineeringTaskEventLabel(value);
}

interface AgentPanelProps {
  context: EngineeringDomain;
  connection: ConnectionState;
  suggestedPrompt?: string;
  onSend: (text: string) => boolean;
  onCancel?: () => void;
  onPreview?: () => void;
  onCollapse?: () => void;
  embedded?: boolean;
}

export default function AgentPanel({ context, connection, suggestedPrompt = "", onSend, onCancel, onPreview, onCollapse, embedded = false }: AgentPanelProps) {
  const { translate } = useI18n();
  const panel = useSessionStore((state) => state.getActivePanel());
  const [input, setInput] = useState(suggestedPrompt);
  const [pendingRequest, setPendingRequest] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmationPending, setConfirmationPending] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const messages = useMemo(() => panel.messages.slice(-12), [panel.messages]);
  const suggestions = useMemo(() => buildPromptSuggestions({
    isEmpty: panel.messages.length === 0,
    isGenerating: panel.isGenerating,
    result: panel.result,
  }), [panel.isGenerating, panel.messages.length, panel.result]);
  const resultFormats = useMemo(() => Object.keys(panel.result?.files || {}).map((format) => format.toUpperCase()), [panel.result?.files]);
  const wasCancelled = panel.durable?.taskStatus === "cancelled";
  const structuredError = !wasCancelled && panel.result?.success === false ? panel.result.error : null;
  const errorDetails = structuredError?.details || {};

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [messages, panel.stepHistory.length]);

  const reviewRequest = () => {
    const value = input.trim();
    if (!value) return;
    setPendingRequest(value);
    setError(null);
  };

  const execute = () => {
    if (!pendingRequest) return;
    const request = `当前上下文：${AGENT_CONTEXT_LABELS[context]}。请先限定修改范围，再执行以下请求并验证结果：${pendingRequest}`;
    if (!onSend(request)) {
      setError("实时连接尚未就绪，计划已保留。");
      return;
    }
    useSessionStore.getState().beginGeneration();
    useSessionStore.getState().addMessage({ role: "user", content: pendingRequest });
    setInput("");
    setPendingRequest(null);
    setError(null);
  };

  const submitServerConfirmation = async (accepted: boolean) => {
    const confirmation = panel.durable?.confirmation;
    if (!confirmation || confirmationPending) return;
    setConfirmationPending(true);
    setError(null);
    try {
      await confirmDurableTask(
        confirmation.workflow_run_id,
        accepted,
        accepted ? "" : "用户拒绝当前执行计划",
      );
      // The persisted snapshot/event stream owns removal of this card.
    } catch (cause) {
      const apiError = cause instanceof EngineeringApiError ? cause : null;
      setError(
        apiError?.code === "confirmation_not_waiting"
          ? "任务状态已变化，正在等待最新状态。"
          : cause instanceof Error
            ? cause.message
            : "任务确认失败",
      );
    } finally {
      setConfirmationPending(false);
    }
  };

  return (
    <section className={`ww-agent-panel ${embedded ? "ww-agent-panel--embedded" : ""}`}>
      {embedded ? (
        <header className="ww-pane-header">
          <div className="ww-agent-title"><span className="ww-agent-avatar">A</span><div><p className="ww-pane-eyebrow">工程协作</p><h2>Agent</h2></div></div>
          <div className="flex items-center gap-1"><span className={`ww-connection-dot ${connection === "connected" ? "ww-connection-dot--online" : ""}`} title={connection === "connected" ? "已连接" : "正在连接"} />{onCollapse ? <button aria-label="折叠 Agent" className="workspace-icon-button" onClick={onCollapse} type="button"><Icon name="minus" size={15} /></button> : null}</div>
        </header>
      ) : null}

      <div className="ww-agent-thread">
        <div className="ww-agent-context"><span>当前上下文</span><strong>{AGENT_CONTEXT_LABELS[context]}</strong><span className="ml-auto">{connection === "connected" ? "实时连接" : "连接中"}</span></div>

        {messages.length === 0 ? (
          <div className="ww-agent-empty"><span className="ww-agent-avatar">A</span><p>描述希望分析或修改的内容。请求会先进入确认，再通过现有实时链路提交。</p></div>
        ) : messages.map((message, index) => (
          <article className={`ww-agent-message ww-agent-message--${message.role === "user" ? "user" : "assistant"}`} key={`${message.role}-${index}`}>
            <div className="ww-agent-message__meta">{message.role === "user" ? translate("你") : "CAD Agent"}</div>
            <div className="ww-agent-message__body">{message.content}</div>
          </article>
        ))}

        {panel.result?.success && resultFormats.length ? (
          <article className="ww-agent-artifact-card">
            <div className="ww-agent-artifact-card__main">
              <span className="ww-agent-artifact-card__icon"><Icon name="box" size={16} /></span>
              <span className="min-w-0 flex-1"><strong>{panel.title || "当前 CAD 模型"}</strong><small>参数化 CAD · {panel.result.parameters?.length || Object.keys(panel.result.params || {}).length} 个参数 · {resultFormats.join(" / ")}</small></span>
              {panel.result.version ? <span className="ww-agent-version">v{panel.result.version}</span> : null}
            </div>
            {onPreview ? <button className="workspace-button" onClick={onPreview} type="button"><Icon name="box" size={14} />查看模型</button> : null}
          </article>
        ) : null}

        {panel.activeRun || panel.artifactUpdates.length ? (
          <div className="ww-agent-run">
            <div className="flex flex-wrap items-center gap-1.5"><strong>Agent 运行</strong><span>{shortId(panel.activeRun?.run_id)}</span><span>{runStatusLabel(panel.activeRun?.status)}</span>{panel.artifactUpdates.slice(-4).map((artifact, index) => <span className="ww-agent-artifact" key={`${artifact.path}:summary:${index}`}>产物 {artifact.artifact_type.toUpperCase()}</span>)}</div>
            {panel.stepHistory.length ? <ol>{panel.stepHistory.slice(-4).map((step, index) => <li key={`${step.timestamp}-${index}`}><span>{stepLabel(step.step)}</span> · {taskText(step.message)}</li>)}</ol> : null}
          </div>
        ) : null}

        {panel.isGenerating ? (
          <div className="ww-agent-progress" role="status"><span className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-[var(--agent)] border-t-transparent" /><div><strong>{taskText(panel.currentStep?.message || "正在执行计划")}</strong><span>运行 {shortId(panel.activeRun?.run_id)} · 实时接收步骤与产物事件</span></div>{onCancel ? <button className="workspace-button" disabled={connection !== "connected" || !panel.durable?.workflowRunId} onClick={onCancel} type="button">取消生成</button> : null}</div>
        ) : null}

        {panel.durable?.confirmation ? (
          <div className="ww-agent-confirmation ww-agent-confirmation--server" role="group" aria-label="服务端执行计划确认">
            <p>执行计划等待确认</p>
            <dl>
              <div><dt>原因</dt><dd>{panel.durable.confirmation.reason}</dd></div>
              <div>
                <dt>影响对象</dt>
                <dd>{panel.durable.confirmation.affected_objects.map((item) => `${item.label}（${item.change}）`).join("、")}</dd>
              </div>
              <div><dt>计划校验</dt><dd><code>{panel.durable.confirmation.plan_hash.slice(0, 12)}</code></dd></div>
            </dl>
            <span>确认结果将直接发送到当前持久工作流。</span>
            <div className="mt-3 flex justify-end gap-2">
              <button className="workspace-button" disabled={confirmationPending} onClick={() => void submitServerConfirmation(false)} type="button">拒绝</button>
              <button className="workspace-button workspace-button--primary" disabled={confirmationPending} onClick={() => void submitServerConfirmation(true)} type="button">{confirmationPending ? "提交中…" : "确认并继续"}</button>
            </div>
          </div>
        ) : null}

        {wasCancelled ? <div className="ww-agent-confirmation" role="status"><p>任务已取消</p><span>后端已确认取消，已有模型和版本保持不变。</span></div> : null}

        {structuredError ? (
          <div className="ww-agent-confirmation ww-agent-confirmation--error" role="alert">
            <p>{translate(structuredErrorLabel(structuredError.type))}</p>
            <dl>
              <div><dt>错误码</dt><dd><code>{structuredError.type}</code></dd></div>
              {typeof errorDetails.operation_id === "string" ? <div><dt>失败操作</dt><dd><code>{errorDetails.operation_id}</code></dd></div> : null}
              {typeof errorDetails.action === "string" ? <div><dt>建议动作</dt><dd>{errorDetails.action}</dd></div> : null}
              {typeof errorDetails.constraint_status === "string" ? <div><dt>约束状态</dt><dd>{errorDetails.constraint_status}</dd></div> : null}
            </dl>
            <span>{structuredError.message}</span>
          </div>
        ) : null}

        {pendingRequest ? (
          <div className="ww-agent-confirmation">
            <p>待确认请求</p>
            <dl><div><dt>当前模块</dt><dd>{AGENT_CONTEXT_LABELS[context]}</dd></div><div><dt>提交内容</dt><dd>{pendingRequest}</dd></div></dl>
            <span>确认后才会调用后端，具体操作计划以服务端实时返回为准。</span>
            <div className="mt-3 flex justify-end gap-2"><button className="workspace-button" onClick={() => setPendingRequest(null)} type="button">返回修改</button><button className="workspace-button workspace-button--primary" onClick={execute} type="button">确认并执行</button></div>
          </div>
        ) : null}

        {error ? <div className="ww-agent-error" role="alert">{error}</div> : null}
        <div ref={endRef} />
      </div>

      <div className="ww-agent-composer-wrap">
        <SuggestionPills disabled={panel.isGenerating} onSelect={(suggestion) => setInput(suggestion.prompt)} suggestions={suggestions} />
        <div className="ww-agent-composer">
          <textarea aria-label="询问 Agent" disabled={panel.isGenerating} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") reviewRequest(); }} placeholder="继续迭代当前设计…" rows={3} value={input} />
          <div className="ww-agent-composer__toolbar"><div className="ww-agent-composer__modes"><span><Icon name="box" size={12} />参数化</span><span>{panel.durable?.workflowRunId ? "Agent V2" : "Agent"}</span></div><span className="ww-agent-composer__shortcut">⌘ Enter 审查</span><button aria-label="审查请求" className="ww-agent-send" disabled={!input.trim() || panel.isGenerating} onClick={reviewRequest} type="button"><Icon name="send" size={15} /></button></div>
        </div>
      </div>
    </section>
  );
}
