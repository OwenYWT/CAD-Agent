import { useEffect, useMemo, useRef, useState } from "react";
import type { ConnectionState } from "../../hooks/useWebSocket";
import { useSessionStore } from "../../stores/sessionStore";
import type { EngineeringDomain } from "../../types/engineering";
import { buildPromptSuggestions } from "../../utils/suggestions";
import SuggestionPills from "../SuggestionPills";
import { WorkspaceDrawer } from "../common/WorkspaceOverlay";
import { Icon } from "../ui/Icon";

const CONTEXT: Record<EngineeringDomain, string> = {
  overview: "项目流程",
  mechanical: "机械设计",
  electronics: "电子设计",
  simulation: "仿真验证",
  firmware: "固件",
};

function shortId(value?: string | null) {
  return value ? value.slice(0, 8) : "-";
}

function artifactLabel(type: string) {
  return type.toUpperCase();
}

const RUN_STATUS_LABELS: Record<string, string> = {
  running: "\u8fd0\u884c\u4e2d",
  succeeded: "\u5df2\u6210\u529f",
  failed: "\u5df2\u5931\u8d25",
  blocked: "\u5f85\u5904\u7406",
  cancelled: "\u5df2\u53d6\u6d88",
  observed: "\u5df2\u89c2\u6d4b",
};

const STEP_LABELS: Record<string, string> = {
  plan_design: "\u9700\u6c42\u89c4\u5212",
  generate_cad_code: "\u751f\u6210 CAD \u4ee3\u7801",
  executing_code: "\u6267\u884c\u5efa\u6a21",
  execute_code: "\u6267\u884c\u4ee3\u7801",
  execute_cad_code: "\u6267\u884c CAD \u4ee3\u7801",
  repair_code: "\u81ea\u52a8\u4fee\u590d\u4ee3\u7801",
  resume_available: "\u53ef\u7ee7\u7eed\u4efb\u52a1",
  finalize_result: "\u6574\u7406\u7ed3\u679c",
};

function runStatusLabel(status?: string | null) {
  if (!status) return RUN_STATUS_LABELS.observed;
  return RUN_STATUS_LABELS[status] || status;
}

function stepLabel(step: string) {
  return STEP_LABELS[step] || step.replace(/_/g, " ");
}

interface AgentDrawerProps {
  open: boolean;
  context: EngineeringDomain;
  connection: ConnectionState;
  suggestedPrompt?: string;
  onClose: () => void;
  onSend: (text: string) => boolean;
}

export default function AgentDrawer({ open, context, connection, suggestedPrompt, onClose, onSend }: AgentDrawerProps) {
  const panel = useSessionStore((state) => state.getActivePanel());
  const [input, setInput] = useState(suggestedPrompt || "");
  const [pendingRequest, setPendingRequest] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const messages = useMemo(() => panel.messages.slice(-8), [panel.messages]);
  const suggestions = useMemo(() => buildPromptSuggestions({
    isEmpty: panel.messages.length === 0,
    isGenerating: panel.isGenerating,
    result: panel.result,
  }), [panel.isGenerating, panel.messages.length, panel.result]);

  useEffect(() => { if (open) endRef.current?.scrollIntoView(); }, [messages, open, panel.stepHistory.length]);

  const reviewRequest = () => {
    const value = input.trim();
    if (!value) return;
    setPendingRequest(value);
    setError(null);
  };

  const execute = () => {
    if (!pendingRequest) return;
    const request = `当前上下文：${CONTEXT[context]}。请先限定修改范围，再执行以下请求并验证结果：${pendingRequest}`;
    if (!onSend(request)) { setError("实时连接尚未就绪，计划已保留。"); return; }
    useSessionStore.getState().beginGeneration();
    useSessionStore.getState().addMessage({ role: "user", content: pendingRequest });
    setInput("");
    setPendingRequest(null);
    setError(null);
  };

  return (
    <WorkspaceDrawer description={`当前上下文：${CONTEXT[context]}。请求确认后通过现有 WebSocket 提交，后端规划和执行进度会实时显示。`} footer={<div className="flex gap-2"><textarea aria-label="询问 Agent" className="min-h-20 flex-1 resize-none rounded-lg border border-[var(--line-strong)] p-3 text-sm outline-none focus:border-[var(--focus)]" disabled={panel.isGenerating} onChange={(event) => setInput(event.target.value)} placeholder="描述要分析或修改的内容…" value={input} /><button className="workspace-button workspace-button--primary self-end" disabled={!input.trim() || panel.isGenerating} onClick={reviewRequest} type="button"><Icon name="send" size={15} />审查请求</button></div>} onClose={onClose} open={open} title="询问 Agent">
      <div className="space-y-4 p-5">
        <div className="rounded-lg border border-[var(--line)] bg-[var(--subtle)] px-3 py-2 text-xs text-[var(--muted)]">连接：{connection === "connected" ? "已连接" : "正在连接"} · 模块：{CONTEXT[context]}</div>
        {panel.activeRun || panel.artifactUpdates.length ? <div className="rounded-lg border border-indigo-200 bg-indigo-50 p-3 text-xs text-indigo-900"><div className="flex flex-wrap items-center gap-1.5"><span className="font-semibold">{"\u667a\u80fd\u4f53\u8fd0\u884c"}</span><span className="rounded-full border border-indigo-200 bg-white/70 px-2 py-0.5">{shortId(panel.activeRun?.run_id)}</span><span className="rounded-full border border-indigo-200 bg-white/70 px-2 py-0.5">{runStatusLabel(panel.activeRun?.status)}</span>{panel.artifactUpdates.slice(-6).map((artifact, index) => <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-emerald-700" key={`${artifact.path}:summary:${index}`}>{"\u4ea7\u7269"} {artifactLabel(artifact.artifact_type)}</span>)}</div><p className="mt-1 text-[11px] text-indigo-700">{"\u5df2\u63a5\u6536\u5230\u8fd0\u884c\u521b\u5efa\u3001\u6b65\u9aa4\u8fdb\u5ea6\u548c\u4ea7\u7269\u66f4\u65b0\u4e8b\u4ef6\u3002"}</p></div> : null}
        <SuggestionPills disabled={panel.isGenerating} onSelect={(suggestion) => setInput(suggestion.prompt)} suggestions={suggestions} />
        {messages.length === 0 ? <div className="py-8 text-center text-sm text-[var(--faint)]">当前项目还没有对话。提交请求后，后端规划和执行状态会在这里显示。</div> : messages.map((message, index) => <div className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`} key={`${message.role}-${index}`}><div className={`max-w-[88%] rounded-lg px-3 py-2 text-xs leading-5 ${message.role === "user" ? "bg-[var(--ink)] text-white" : "border border-[var(--line)] bg-white text-[var(--ink)]"}`}>{message.content}</div></div>)}
        {panel.isGenerating ? <div className="rounded-lg border border-sky-200 bg-sky-50 p-3" role="status"><div className="flex items-center gap-2 text-xs font-medium text-sky-900"><span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-sky-600 border-t-transparent" />{panel.currentStep?.message || "\u6b63\u5728\u6267\u884c\u8ba1\u5212"}</div><div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px] text-sky-800"><span className="rounded-full border border-sky-200 bg-white/70 px-2 py-0.5">{"\u8fd0\u884c"} {shortId(panel.activeRun?.run_id)}</span><span className="rounded-full border border-sky-200 bg-white/70 px-2 py-0.5">{"\u667a\u80fd\u4f53\u6b65\u9aa4\u4e8b\u4ef6"}</span>{panel.artifactUpdates.slice(-4).map((artifact, index) => <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-emerald-700" key={`${artifact.path}-${index}`}>{"\u4ea7\u7269"} {artifactLabel(artifact.artifact_type)}</span>)}</div>{panel.stepHistory.length ? <ol className="mt-2 space-y-1 text-[11px] text-sky-700">{panel.stepHistory.slice(-4).map((step, index) => <li key={`${step.timestamp}-${index}`}><span className="text-sky-500">{stepLabel(step.step)}</span> - {step.message}</li>)}</ol> : null}</div> : null}
        {pendingRequest ? <div className="rounded-lg border border-amber-200 bg-amber-50 p-4"><p className="text-xs font-semibold text-amber-900">待确认请求</p><dl className="mt-2 space-y-2 text-xs leading-5 text-amber-900"><div><dt className="font-medium">当前模块</dt><dd>{CONTEXT[context]}</dd></div><div><dt className="font-medium">提交内容</dt><dd>{pendingRequest}</dd></div></dl><p className="mt-2 text-[11px] leading-5 text-amber-800">确认后才会调用后端；具体操作计划以服务端实时返回为准。</p><div className="mt-3 flex justify-end gap-2"><button className="workspace-button" onClick={() => setPendingRequest(null)} type="button">返回修改</button><button className="workspace-button workspace-button--primary" onClick={execute} type="button">确认并执行</button></div></div> : null}
        {error ? <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800" role="alert">{error}</div> : null}
        <div ref={endRef} />
      </div>
    </WorkspaceDrawer>
  );
}
