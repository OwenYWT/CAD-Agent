import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type {
  ChatMessage,
  GenerationResult,
  MultiStepInfo,
  StepUpdate,
} from "../types";
import { createId } from "../lib/createId";

export interface StepHistoryEntry extends StepUpdate {
  timestamp: number;
}

function hasTerminalStep(history: StepHistoryEntry[]) {
  return history.some(
    (entry) =>
      entry.step === "complete" ||
      entry.step === "failed" ||
      entry.status === "failed" ||
      entry.stage_id === "design_confirmation",
  );
}

function terminalStepFromResult(success: boolean, needsConfirmation = false): StepHistoryEntry {
  const timestamp = Date.now();
  if (needsConfirmation) {
    return {
      step: "planning",
      message: "\u8bbe\u8ba1\u7b80\u62a5\u9700\u8981\u5148\u786e\u8ba4",
      status: "warn",
      stage_id: "design_confirmation",
      started_at: new Date(timestamp).toISOString(),
      duration_ms: null,
      detail: { source: "frontend_result" },
      timestamp,
    };
  }
  return {
    step: success ? "complete" : "failed",
    message: success ? "\u751f\u6210\u5b8c\u6210" : "\u751f\u6210\u5931\u8d25",
    status: success ? "success" : "failed",
    stage_id: success ? "complete" : "failed",
    started_at: new Date(timestamp).toISOString(),
    duration_ms: null,
    detail: { source: "frontend_result" },
    timestamp,
  };
}

export interface PanelState {
  id: string;
  title: string;
  messages: ChatMessage[];
  currentStep: StepUpdate | null;
  result: GenerationResult | null;
  isGenerating: boolean;
  stepHistory: StepHistoryEntry[];
  generationStartTime: number | null;
  baselineVersion: number;
  multiStepProgress: MultiStepInfo[] | null;
  lastError: string | null;
}

function createPanel(title = "新对话"): PanelState {
  return {
    id: createId(),
    title,
    messages: [],
    currentStep: null,
    result: null,
    isGenerating: false,
    stepHistory: [],
    generationStartTime: null,
    baselineVersion: 0,
    multiStepProgress: null,
    lastError: null,
  };
}

interface SessionState {
  ownerId: string | null;
  sessionId: string;
  panels: PanelState[];
  activePanelId: string;
  bindOwner: (ownerId: string | null) => void;

  // Panel management
  addPanel: (title?: string) => string; // returns new panel id
  removePanel: (id: string) => void;
  switchPanel: (id: string) => void;
  renamePanel: (id: string, title: string) => void;

  // Active panel helpers (read-only convenience)
  getActivePanel: () => PanelState;

  // Actions (operate on active panel)
  addMessage: (msg: ChatMessage) => void;
  beginGeneration: (message?: string) => void;
  setStep: (step: StepUpdate | null, panelId?: string) => void;
  setResult: (result: GenerationResult, panelId?: string) => void;
  restorePanelResult: (panelId: string, result: GenerationResult, code: string) => void;
  setError: (error: string, panelId?: string) => void;
  reset: () => void;

  // Hydrate a panel from history
  hydratePanel: (
    panelId: string,
    messages: ChatMessage[],
    code?: string | null,
  ) => void;

  // Load full session from backend
  loadSession: (
    sessionId: string,
    panels: { id: string; title: string; messages: ChatMessage[]; currentCode?: string | null }[],
  ) => void;
}

function updatePanel(
  panels: PanelState[],
  panelId: string,
  updater: (panel: PanelState) => Partial<PanelState>,
): PanelState[] {
  return panels.map((p) =>
    p.id === panelId ? { ...p, ...updater(p) } : p,
  );
}

function restoreGenerationResult(
  messages: ChatMessage[],
  currentCode?: string | null,
): GenerationResult | null {
  const stored = [...messages]
    .reverse()
    .find((message) => message.result?.success)
    ?.result;
  if (stored) {
    return currentCode && !stored.code ? { ...stored, code: currentCode } : stored;
  }
  return currentCode
    ? ({ success: true, code: currentCode } as GenerationResult)
    : null;
}

function restoreLastError(messages: ChatMessage[]): string | null {
  const latest = [...messages].reverse().find((message) => message.result);
  if (!latest?.result || latest.result.success) return null;
  return latest.result.error?.message || "任务执行失败";
}

const defaultPanel = createPanel("对话 1");

function freshSession(ownerId: string | null) {
  const panel = createPanel("对话 1");
  return {
    ownerId,
    sessionId: createId(),
    panels: [panel],
    activePanelId: panel.id,
  };
}

export const useSessionStore = create<SessionState>()(persist((set, get) => ({
  ownerId: null,
  sessionId: createId(),
  panels: [defaultPanel],
  activePanelId: defaultPanel.id,

  bindOwner: (ownerId) =>
    set((state) => (
      state.ownerId === ownerId
        ? state
        : freshSession(ownerId)
    )),

  getActivePanel: () => {
    const state = get();
    return (
      state.panels.find((p) => p.id === state.activePanelId) || state.panels[0]
    );
  },

  addPanel: (title) => {
    const idx = get().panels.length + 1;
    const panel = createPanel(title || `对话 ${idx}`);
    set((state) => ({
      panels: [...state.panels, panel],
      activePanelId: panel.id,
    }));
    return panel.id;
  },

  removePanel: (id) =>
    set((state) => {
      if (state.panels.length <= 1) return state; // keep at least one
      const remaining = state.panels.filter((p) => p.id !== id);
      const newActive =
        state.activePanelId === id
          ? remaining[remaining.length - 1].id
          : state.activePanelId;
      return { panels: remaining, activePanelId: newActive };
    }),

  switchPanel: (id) => set({ activePanelId: id }),

  renamePanel: (id, title) =>
    set((state) => ({
      panels: updatePanel(state.panels, id, () => ({ title })),
    })),

  addMessage: (msg) =>
    set((state) => ({
      panels: updatePanel(state.panels, state.activePanelId, (p) => ({
        messages: [...p.messages, msg],
      })),
    })),

  beginGeneration: (message = "正在理解建模需求") =>
    set((state) => ({
      panels: updatePanel(state.panels, state.activePanelId, (panel) => ({
        isGenerating: true,
        currentStep: { step: "planning", message },
        stepHistory: [],
        generationStartTime: Date.now(),
        baselineVersion: panel.baselineVersion + 1,
        lastError: null,
      })),
    })),

  setStep: (step, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (p) => {
          const now = Date.now();
          const base: Partial<PanelState> = {
            currentStep: step,
            isGenerating: step !== null,
            generationStartTime: p.generationStartTime ?? (step ? now : null),
            stepHistory: step
              ? [...p.stepHistory, { ...step, timestamp: now }]
              : p.stepHistory,
          };

          // Track multi-step progress
          if (step?.step === "multi_step") {
            const match = step.message.match(/步骤 (\d+)\/(\d+): (.+)/);
            if (match) {
              const current = parseInt(match[1]);
              const total = parseInt(match[2]);
              const desc = match[3];
              const steps: MultiStepInfo[] = Array.from(
                { length: total },
                (_, i) => ({
                  phase:
                    i < current - 1
                      ? "done"
                      : i === current - 1
                        ? "running"
                        : "pending",
                  description: i === current - 1 ? desc : `步骤 ${i + 1}`,
                  status:
                    i < current - 1
                      ? ("done" as const)
                      : i === current - 1
                        ? ("running" as const)
                        : ("pending" as const),
                }),
              );
              const prev = p.multiStepProgress;
              if (prev) {
                for (
                  let i = 0;
                  i < Math.min(prev.length, steps.length);
                  i++
                ) {
                  if (prev[i].description !== `步骤 ${i + 1}`) {
                    steps[i].description = prev[i].description;
                  }
                }
              }
              return { ...base, multiStepProgress: steps };
            }
          }
          return base;
        }),
      };
    }),

  setResult: (result, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (p) => {
          const assistantMsg: ChatMessage = {
            role: "assistant",
            content: result.needs_confirmation
              ? "设计简报需要确认"
              : result.success
                ? "CAD 模型已生成"
              : `生成失败: ${result.error?.message || "未知错误"}`,
            result,
          };
          const nextStepHistory = hasTerminalStep(p.stepHistory)
            ? p.stepHistory
            : [...p.stepHistory, terminalStepFromResult(Boolean(result.success), Boolean(result.needs_confirmation))];

          return {
            result: result.success || result.needs_confirmation ? result : p.result,
            lastError: result.success || result.needs_confirmation ? null : result.error?.message || "任务执行失败",
            isGenerating: false,
            currentStep: null,
            multiStepProgress: null,
            stepHistory: nextStepHistory,
            generationStartTime: null,
            messages: [...p.messages, assistantMsg],
          };
        }),
      };
    }),

  restorePanelResult: (panelId, result, code) =>
    set((state) => ({
      panels: updatePanel(state.panels, panelId, (panel) => {
        const restoredResult = { ...result, code };
        const restoreMessage: ChatMessage = {
          role: "assistant",
          content: `\u5df2\u6062\u590d\u7248\u672c ${result.version ?? ""}`.trim(),
          result: restoredResult,
        };
        return {
          result: restoredResult,
          isGenerating: false,
          currentStep: null,
          multiStepProgress: null,
          stepHistory: [],
          generationStartTime: null,
          messages: [...panel.messages, restoreMessage],
        };
      }),
    })),

  setError: (error, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (p) => {
          const errorMsg: ChatMessage = {
            role: "assistant",
            content: `错误: ${error}`,
          };
          const nextStepHistory = hasTerminalStep(p.stepHistory)
            ? p.stepHistory
            : [...p.stepHistory, terminalStepFromResult(false)];

          return {
            isGenerating: false,
            currentStep: null,
            stepHistory: nextStepHistory,
            generationStartTime: null,
            lastError: error,
            messages: [...p.messages, errorMsg],
          };
        }),
      };
    }),

  reset: () => {
    set(freshSession(get().ownerId));
  },

  hydratePanel: (panelId, messages, code) =>
    set((state) => ({
      panels: updatePanel(state.panels, panelId, () => ({
        messages,
        result: restoreGenerationResult(messages, code),
        lastError: restoreLastError(messages),
        baselineVersion: 1,
      })),
    })),

  loadSession: (sessionId, panels) => {
    const panelStates = panels.map((p) => ({
      ...createPanel(p.title),
      id: p.id,
      messages: p.messages,
      result: restoreGenerationResult(p.messages, p.currentCode),
      lastError: restoreLastError(p.messages),
      baselineVersion: 1,
    }));
    set({
      sessionId,
      panels: panelStates.length > 0 ? panelStates : [createPanel("对话 1")],
      activePanelId: panelStates.length > 0 ? panelStates[0].id : createId(),
    });
  },
}), {
  name: "wordswave-engineering-session",
  storage: createJSONStorage(() => sessionStorage),
  partialize: (state) => ({
    ownerId: state.ownerId,
    sessionId: state.sessionId,
    panels: state.panels,
    activePanelId: state.activePanelId,
  }),
}));
