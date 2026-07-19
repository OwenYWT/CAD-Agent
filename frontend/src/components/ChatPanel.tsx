import { useState, useRef, useEffect, useCallback } from "react";
import { useSessionStore } from "../stores/sessionStore";
import type { ChatMessage, GenerationResult, ManufacturingProfile, RecoveryAction } from "../types";
import SuggestionPills from "./SuggestionPills";
import { buildPromptSuggestions, type PromptSuggestion } from "../utils/suggestions";
import {
  isSupportedReferenceAttachment,
  summarizeReferenceAttachment,
  withReferenceAttachmentsPrompt,
  type ReferenceAttachmentSummary,
} from "../utils/referenceAttachments";
import {
  formatManufacturingProfile,
  getManufacturingProfilePreset,
  MANUFACTURING_PROFILE_PRESETS,
} from "../utils/manufacturingProfiles";

import { authFetch } from "../auth";
const API_BASE = import.meta.env.VITE_API_BASE || "";

interface ChatPanelProps {
  onSendMessage: (text: string, manufacturingProfile?: ManufacturingProfile | null) => void;
  onCancel?: () => void;
  onSwitchTab?: (tab: string) => void;
}

const EXAMPLES = [
  { title: "手机支架", desc: "桌面手机支架，可放横竖屏", prompt: "设计一个桌面手机支架，底座稳固，可以放横屏和竖屏，适合 3D 打印" },
  { title: "耳机挂钩", desc: "挂在桌边的耳机架", prompt: "设计一个挂在桌沿的耳机挂钩，挂耳处宽 40mm，能卡在 25mm 厚的桌板上" },
  { title: "桌面收纳盒", desc: "宽 60 深 40 高 30mm，分两格", prompt: "设计一个桌面收纳盒，宽 60mm 深 40mm 高 30mm，内部分成两格，壁厚 1.5mm" },
  { title: "钥匙扣", desc: "可刻字的圆角钥匙扣", prompt: "设计一个圆角矩形钥匙扣，长 40mm 宽 20mm 厚 3mm，一端有 5mm 挂孔" },
];

const STEP_ICONS: Record<string, string> = {
  planning: "\u{1F914}",
  retrieving_examples: "\u{1F4DA}",
  generating_code: "\u{1F4BB}",
  executing: "\u2699\uFE0F",
  fixing_error: "\u{1F527}",
  multi_step: "\u{1F9E9}",
  assembly_part: "\u{1F9F1}",
  complete: "\u2705",
  failed: "\u274C",
};

function PrintBadges({ result }: { result: GenerationResult }) {
  const v = result.validation;
  if (!v) return null;
  // Only show printability badges once the backend evaluated them (print gate).
  if (v.printable === undefined || v.printable === null) return null;

  const badge = (ok: boolean, label: string) => (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
        ok ? "bg-emerald-100 text-emerald-700" : "bg-amber-100 text-amber-700"
      }`}
    >
      {ok ? "✓" : "!"} {label}
    </span>
  );

  return (
    <div className="flex flex-wrap gap-1">
      {badge(!!v.is_watertight, "水密")}
      {badge(!!v.fits_build_volume, "可上盘")}
      {v.min_wall_thickness != null &&
        badge(v.min_wall_thickness >= 0.8, `壁厚 ${v.min_wall_thickness.toFixed(1)}mm`)}
    </div>
  );
}

function RequirementBrief({ result }: { result: GenerationResult }) {
  const [open, setOpen] = useState(false);
  const plan = result.plan;
  if (!plan) return null;

  const dims = Object.entries(plan.dimensions ?? {});
  const feats = plan.features ?? [];
  const ambig = plan.ambiguities ?? [];

  return (
    <div className="border-t border-emerald-100 pt-1 mt-1">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-[10px] text-gray-600 hover:text-gray-800 w-full"
      >
        <span>🧠</span>
        <span className="font-medium">理解到的需求</span>
        {ambig.length > 0 && <span className="text-amber-600">· {ambig.length} 处待确认</span>}
        <span className="ml-auto text-gray-400">{open ? "收起" : "展开"}</span>
      </button>
      {open && (
        <div className="mt-1 space-y-1 text-[10px] text-gray-600">
          <div>
            <span className="text-gray-400">类型: </span>
            {plan.part_type}
            {plan.modeling_hint ? ` · ${plan.modeling_hint}` : ""}
          </div>
          {dims.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {dims.map(([k, v]) => (
                <span key={k} className="px-1 bg-white border border-emerald-200 rounded">
                  {k}: {v}
                </span>
              ))}
            </div>
          )}
          {feats.length > 0 && (
            <div>
              <span className="text-gray-400">特征: </span>
              {feats.join("、")}
            </div>
          )}
          {ambig.length > 0 && (
            <ul className="text-amber-700 list-disc list-inside">
              {ambig.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function InspectChecks({ result }: { result: GenerationResult }) {
  const [open, setOpen] = useState(false);
  const report = result.inspect_report;
  if (!report || report.checks.length === 0) return null;

  const dot: Record<string, string> = {
    pass: "bg-emerald-500",
    warn: "bg-amber-500",
    fail: "bg-red-500",
  };
  const verdictLabel: Record<string, string> = { pass: "通过", warn: "有提示", fail: "不通过" };
  const verdictColor: Record<string, string> = {
    pass: "text-emerald-700",
    warn: "text-amber-700",
    fail: "text-red-700",
  };

  return (
    <div className="border-t border-emerald-100 pt-1 mt-1">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-[10px] text-gray-600 hover:text-gray-800 w-full"
      >
        <span className={`inline-block w-1.5 h-1.5 rounded-full ${dot[report.verdict]}`} />
        <span className="font-medium">检查报告</span>
        <span className={verdictColor[report.verdict]}>{verdictLabel[report.verdict]}</span>
        {report.design_score != null && (
          <span className="text-gray-400">· DFM {report.design_score}/100</span>
        )}
        <span className="ml-auto text-gray-400">{open ? "收起" : "展开"}</span>
      </button>
      {open && (
        <ul className="mt-1 space-y-0.5">
          {report.checks.map((c, i) => (
            <li key={i} className="flex items-start gap-1.5 text-[10px] text-gray-600">
              <span className={`mt-1 inline-block w-1.5 h-1.5 rounded-full shrink-0 ${dot[c.status]}`} />
              <span className="text-gray-400 shrink-0">{c.name}</span>
              <span className="text-gray-600">{c.message}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FeedbackChip({ requestId }: { requestId: string }) {
  const [sent, setSent] = useState<string | null>(null);

  const send = async (printed: string, rating?: string) => {
    setSent(printed);
    try {
      await authFetch(`${API_BASE}/api/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, printed, rating }),
      });
    } catch {
      /* best-effort: feedback failure must never break the session */
    }
  };

  if (sent) {
    return <div className="text-[10px] text-gray-400 pt-1">感谢反馈 🙏</div>;
  }

  return (
    <div className="flex items-center gap-1.5 pt-1 border-t border-emerald-100 mt-1">
      <span className="text-[10px] text-gray-500">打出来了吗?</span>
      <button
        onClick={() => send("yes", "up")}
        className="text-[10px] px-1.5 py-0.5 bg-white border border-emerald-300 text-emerald-700 rounded hover:bg-emerald-100"
      >
        👍 打成功了
      </button>
      <button
        onClick={() => send("no", "down")}
        className="text-[10px] px-1.5 py-0.5 bg-white border border-gray-300 text-gray-600 rounded hover:bg-gray-100"
      >
        👎 没打出来
      </button>
      <button
        onClick={() => send("not_yet")}
        className="text-[10px] px-1.5 py-0.5 bg-white border border-gray-200 text-gray-400 rounded hover:bg-gray-100"
      >
        还没打
      </button>
    </div>
  );
}

const RECOVERY_ACTION_LABELS: Record<RecoveryAction["action_type"], string> = {
  retry_simpler: "\u7b80\u5316\u91cd\u8bd5",
  fix_printability: "\u4f18\u5316\u53ef\u6253\u5370\u6027",
  clarify: "\u56de\u7b54\u5f85\u786e\u8ba4\u9879",
  explain: "\u89e3\u91ca\u5931\u8d25\u539f\u56e0",
  inspect: "\u67e5\u770b\u68c0\u67e5\u95ee\u9898",
};

function RecoveryActionButtons({
  actions,
  onSelect,
}: {
  actions?: RecoveryAction[] | null;
  onSelect?: (action: RecoveryAction) => void;
}) {
  if (!actions?.length || !onSelect) return null;

  return (
    <div className="flex flex-wrap gap-1.5 pt-1">
      {actions.map((action) => (
        <button
          key={`${action.action_type}-${RECOVERY_ACTION_LABELS[action.action_type] || action.label}`}
          type="button"
          onClick={() => onSelect(action)}
          title={action.reason || action.prompt}
          className="text-xs px-2 py-1 bg-white border border-amber-300 text-amber-700 rounded hover:bg-amber-100 transition-colors"
        >
          {RECOVERY_ACTION_LABELS[action.action_type] || action.label}
        </button>
      ))}
    </div>
  );
}

function ResultCard({
  result,
  onSwitchTab,
  onRecoveryActionSelect,
}: {
  result: GenerationResult;
  onSwitchTab?: (tab: string) => void;
  onRecoveryActionSelect?: (action: RecoveryAction) => void;
}) {
  if (result.needs_confirmation) {
    const questions = result.design_brief?.open_questions || result.plan?.design_brief?.open_questions || [];
    return (
      <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 space-y-2">
        <div className="flex items-center gap-1.5 text-amber-700 text-sm font-medium">
          <span>&#9888;</span> {"\u9700\u8981\u786e\u8ba4\u9700\u6c42"}
        </div>
        <p className="text-xs text-amber-700">{"\u8bf7\u5148\u56de\u7b54\u5f85\u786e\u8ba4\u95ee\u9898\uff0c\u518d\u7ee7\u7eed\u751f\u6210 CAD \u6a21\u578b\u3002"}</p>
        {questions.length > 0 && (
          <ul className="text-xs text-amber-700 space-y-0.5 list-disc list-inside">
            {questions.map((question, index) => (
              <li key={index}>{question}</li>
            ))}
          </ul>
        )}
        <div className="flex flex-wrap gap-1.5">
          {onSwitchTab && (
            <button
              onClick={() => onSwitchTab("analysis")}
              className="text-xs px-2 py-1 bg-white border border-amber-300 text-amber-700 rounded hover:bg-amber-100 transition-colors"
            >
              {"\u67e5\u770b\u8bbe\u8ba1\u7b80\u62a5"}
            </button>
          )}
          <RecoveryActionButtons actions={result.recovery_actions} onSelect={onRecoveryActionSelect} />
        </div>
      </div>
    );
  }

  if (!result.success) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-3 space-y-1">
        <div className="flex items-center gap-1.5 text-red-700 text-sm font-medium">
          <span>&#10007;</span> {"\u751f\u6210\u5931\u8d25"}
        </div>
        <p className="text-xs text-red-600">{result.error?.message || "Unknown error"}</p>
        <RecoveryActionButtons actions={result.recovery_actions} onSelect={onRecoveryActionSelect} />
      </div>
    );
  }

  const bb = result.validation?.bounding_box;
  const dims = bb
    ? `${(bb.x_max - bb.x_min).toFixed(1)} x ${(bb.y_max - bb.y_min).toFixed(1)} x ${(bb.z_max - bb.z_min).toFixed(1)} mm`
    : null;
  const vol = result.validation?.volume;
  const warnings = result.validation?.print_warnings ?? [];
  // success=true means a model was produced; printable===false means it won't slice/print
  // as-is. Keep the preview (card stays green-bordered) but make the header honest.
  const notPrintable = result.validation?.printable === false;

  return (
    <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-3 space-y-2">
      {notPrintable ? (
        <div className="flex items-center gap-1.5 text-amber-700 text-sm font-medium">
          <span>&#9888;</span> 模型已生成（不可直接打印，见下方提示）
        </div>
      ) : (
        <div className="flex items-center gap-1.5 text-emerald-700 text-sm font-medium">
          <span>&#10003;</span> 模型已生成
        </div>
      )}

      {(dims || (vol && vol > 0)) && (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-emerald-600">
          {dims && <span>尺寸: {dims}</span>}
          {vol !== undefined && vol > 0 && <span>体积: {vol.toFixed(0)} mm³</span>}
        </div>
      )}

      <PrintBadges result={result} />

      {warnings.length > 0 && (
        <ul className="text-[10px] text-amber-700 space-y-0.5 list-disc list-inside">
          {warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}

      <RecoveryActionButtons actions={result.recovery_actions} onSelect={onRecoveryActionSelect} />

      {onSwitchTab && (
        <div className="flex gap-1.5 pt-1">
          {result.params && Object.keys(result.params).length > 0 && (
            <button
              onClick={() => onSwitchTab("params")}
              className="text-xs px-2 py-1 bg-white border border-emerald-300 text-emerald-700 rounded hover:bg-emerald-100 transition-colors"
            >
              调整参数
            </button>
          )}
          <button
            onClick={() => onSwitchTab("analysis")}
            className="text-xs px-2 py-1 bg-white border border-emerald-300 text-emerald-700 rounded hover:bg-emerald-100 transition-colors"
          >
            DFM 分析
          </button>
          <button
            onClick={() => onSwitchTab("download")}
            className="text-xs px-2 py-1 bg-white border border-emerald-300 text-emerald-700 rounded hover:bg-emerald-100 transition-colors"
          >
            下载文件
          </button>
        </div>
      )}

      <RequirementBrief result={result} />

      <InspectChecks result={result} />

      {result.request_id && <FeedbackChip requestId={result.request_id} />}
    </div>
  );
}

function MessageBubble({
  msg,
  onSwitchTab,
  onRecoveryActionSelect,
}: {
  msg: ChatMessage;
  onSwitchTab?: (tab: string) => void;
  onRecoveryActionSelect?: (action: RecoveryAction) => void;
}) {
  const [showCode, setShowCode] = useState(false);
  const isUser = msg.role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[85%] ${isUser ? "" : "w-full"}`}>
        {/* User message */}
        {isUser && (
          <div className="bg-indigo-600 text-white rounded-lg px-3 py-2 text-sm">
            <p className="whitespace-pre-wrap">{msg.content}</p>
          </div>
        )}

        {/* Assistant message */}
        {!isUser && (
          <div className="space-y-2">
            {msg.result ? (
              <ResultCard
                result={msg.result}
                onSwitchTab={onSwitchTab}
                onRecoveryActionSelect={onRecoveryActionSelect}
              />
            ) : (
              <div className="bg-gray-100 text-gray-800 rounded-lg px-3 py-2 text-sm">
                <p className="whitespace-pre-wrap">{msg.content}</p>
              </div>
            )}

            {msg.result?.code && (
              <>
                <button
                  className="text-xs text-indigo-500 hover:text-indigo-700"
                  onClick={() => setShowCode(!showCode)}
                >
                  {showCode ? "隐藏代码" : "查看代码"}
                </button>
                {showCode && (
                  <pre className="p-3 bg-gray-900 text-green-300 rounded-lg text-xs overflow-x-auto max-h-48 overflow-y-auto">
                    {msg.result.code}
                  </pre>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function InlineProgress() {
  const panel = useSessionStore((s) => s.getActivePanel());
  const { stepHistory, generationStartTime, isGenerating } = panel;

  if (!isGenerating || stepHistory.length === 0) return null;

  const lastStep = stepHistory[stepHistory.length - 1];
  const icon = STEP_ICONS[lastStep.step] || "\u23F3";
  const elapsed = generationStartTime
    ? `${Math.round((Date.now() - generationStartTime) / 1000)}s`
    : "";

  return (
    <div className="flex items-start gap-2 px-1">
      <div className="bg-indigo-50 border border-indigo-100 rounded-lg px-3 py-2 space-y-1 w-full">
        {/* Show last few steps */}
        {stepHistory.slice(-3).map((entry, i) => {
          const isLast = i === stepHistory.slice(-3).length - 1;
          const stepIcon = isLast ? icon : "\u2705";
          return (
            <div
              key={i}
              className={`flex items-center gap-2 text-xs ${
                isLast ? "text-indigo-700 font-medium" : "text-gray-400"
              }`}
            >
              <span className="shrink-0 w-4 text-center">{stepIcon}</span>
              <span className="flex-1 truncate">{entry.message}</span>
              {isLast && (
                <>
                  <span className="text-indigo-400 tabular-nums text-[10px]">{elapsed}</span>
                  <span className="shrink-0 inline-block w-3 h-3 border-2 border-indigo-400 border-t-transparent rounded-full animate-spin" />
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function ChatPanel({ onSendMessage, onSwitchTab }: ChatPanelProps) {
  const panel = useSessionStore((s) => s.getActivePanel());
  const { messages, isGenerating } = panel;
  const [input, setInput] = useState("");
  const [selectedProfileId, setSelectedProfileId] = useState("fdm-pla");
  const [referenceAttachments, setReferenceAttachments] = useState<ReferenceAttachmentSummary[]>([]);
  const selectedProfile = getManufacturingProfilePreset(selectedProfileId);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, panel.stepHistory.length]);

  // Auto-resize textarea
  const adjustHeight = useCallback(() => {
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 120) + "px";
    }
  }, []);

  useEffect(() => {
    adjustHeight();
  }, [input, adjustHeight]);

  const handleSend = () => {
    const text = input.trim();
    if ((!text && referenceAttachments.length === 0) || isGenerating) return;
    const prompt = withReferenceAttachmentsPrompt(text, referenceAttachments);
    useSessionStore.getState().addMessage({ role: "user", content: prompt });
    onSendMessage(prompt, selectedProfile.profile);
    setInput("");
    setReferenceAttachments([]);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleExampleClick = (prompt: string) => {
    useSessionStore.getState().addMessage({ role: "user", content: prompt });
    onSendMessage(prompt, selectedProfile.profile);
  };

  const isEmpty = messages.length === 0;
  const latestUserMessage = messages.filter((msg) => msg.role === "user").at(-1)?.content;
  const suggestions = buildPromptSuggestions({
    isEmpty,
    isGenerating,
    result: panel.result,
    latestUserMessage,
  });

  const handleSuggestionSelect = (suggestion: PromptSuggestion) => {
    setInput(suggestion.prompt);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      adjustHeight();
    });
  };

  const handleRecoveryActionSelect = (action: RecoveryAction) => {
    setInput(action.prompt);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      adjustHeight();
    });
  };

  const handleReferenceAttachmentChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    const supportedFiles = files.filter(isSupportedReferenceAttachment).slice(0, 4);
    if (supportedFiles.length > 0) {
      setReferenceAttachments((current) => [
        ...current,
        ...supportedFiles.map(summarizeReferenceAttachment),
      ].slice(0, 4));
    }
    event.target.value = "";
  };

  const removeReferenceAttachment = (index: number) => {
    setReferenceAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index));
  };

  return (
    <div className="flex flex-col h-full bg-white border-r border-gray-200">
      {/* Messages or empty state */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {isEmpty ? (
          /* Empty state with examples */
          <div className="flex flex-col items-center justify-center h-full space-y-6 px-2">
            <div className="text-center space-y-2">
              <div className="text-3xl">🖨️</div>
              <h2 className="text-base font-semibold text-gray-700">AI 建模助手</h2>
              <p className="text-xs text-gray-400">用一句话描述你想要的东西，AI 自动生成可 3D 打印的模型</p>
            </div>
            <div className="w-full grid grid-cols-2 gap-2">
              {EXAMPLES.map((ex) => (
                <button
                  key={ex.title}
                  onClick={() => handleExampleClick(ex.prompt)}
                  className="text-left p-2.5 bg-gray-50 border border-gray-200 rounded-lg hover:border-indigo-300 hover:bg-indigo-50 transition-colors group"
                >
                  <div className="text-xs font-medium text-gray-700 group-hover:text-indigo-700">
                    {ex.title}
                  </div>
                  <div className="text-[10px] text-gray-400 mt-0.5 line-clamp-2">
                    {ex.desc}
                  </div>
                </button>
              ))}
            </div>
          </div>
        ) : (
          /* Message list */
          <>
            {messages.map((msg, i) => (
              <MessageBubble
                key={i}
                msg={msg}
                onSwitchTab={onSwitchTab}
                onRecoveryActionSelect={handleRecoveryActionSelect}
              />
            ))}
            <InlineProgress />
          </>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input area */}
      <div className="border-t border-gray-200 p-3">
        <div className="mb-2 flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 px-2 py-1.5 text-xs">
          <span className="shrink-0 text-gray-500">{"\u5236\u9020\u914d\u7f6e"}</span>
          <select
            value={selectedProfileId}
            onChange={(event) => setSelectedProfileId(event.target.value)}
            disabled={isGenerating}
            className="rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 focus:outline-none focus:ring-1 focus:ring-indigo-400 disabled:opacity-50"
          >
            {MANUFACTURING_PROFILE_PRESETS.map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.label}
              </option>
            ))}
          </select>
          <span className="min-w-0 flex-1 truncate text-[11px] text-gray-400" title={selectedProfile.description}>
            {formatManufacturingProfile(selectedProfile.profile)}
          </span>
        </div>
        <SuggestionPills
          suggestions={suggestions}
          disabled={isGenerating}
          onSelect={handleSuggestionSelect}
        />
        {referenceAttachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {referenceAttachments.map((attachment, index) => (
              <span
                key={`${attachment.name}-${index}`}
                className="inline-flex max-w-full items-center gap-1 rounded-full border border-indigo-100 bg-indigo-50 px-2 py-1 text-[11px] text-indigo-700"
                title={`${attachment.mime_type} - ${attachment.size_label}`}
              >
                <span className="shrink-0">{attachment.category === "image" ? "\u56fe\u7247" : "CAD"}</span>
                <span className="truncate max-w-[160px]">{attachment.name}</span>
                <button
                  type="button"
                  onClick={() => removeReferenceAttachment(index)}
                  className="text-indigo-400 hover:text-indigo-700"
                  aria-label={`Remove ${attachment.name}`}
                >
                  x
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="flex gap-2 items-end">
          <div className="flex-1 relative">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="描述你想要的零件..."
              disabled={isGenerating}
              rows={1}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:border-transparent disabled:opacity-50 resize-none leading-relaxed"
            />
          </div>
          {/* NOTE: the previous "取消" button was a no-op — the backend only ACKs a
              cancel and keeps running, producing a contradictory trailing "已取消" card
              after the real result. Hidden until cooperative cancellation is implemented
              (see DEPLOY/roadmap). While generating we show a non-interactive indicator. */}
          {isGenerating ? (
            <button
              disabled
              className="bg-gray-300 text-white rounded-lg px-4 py-2 text-sm shrink-0 cursor-not-allowed flex items-center gap-1.5"
            >
              <span className="inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin" />
              {"\u751f\u6210\u4e2d"}
            </button>
          ) : (
            <>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".png,.jpg,.jpeg,.webp,.stl,.step,.stp,image/png,image/jpeg,image/webp"
                onChange={handleReferenceAttachmentChange}
                className="hidden"
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                className="rounded-lg border border-gray-300 px-3 py-2 text-xs text-gray-600 hover:border-indigo-300 hover:text-indigo-700 shrink-0 transition-colors"
              >
                {"\u53c2\u8003\u6587\u4ef6"}
              </button>
              <button
                onClick={handleSend}
                disabled={!input.trim() && referenceAttachments.length === 0}
                className="bg-indigo-600 text-white rounded-lg px-4 py-2 text-sm hover:bg-indigo-700 disabled:opacity-40 disabled:cursor-not-allowed shrink-0 transition-colors"
              >
                {"\u53d1\u9001"}
              </button>
            </>
          )}
        </div>
        <div className="text-[10px] text-gray-300 mt-1 text-right">
          Enter 发送 · Shift+Enter 换行
        </div>
      </div>
    </div>
  );
}
