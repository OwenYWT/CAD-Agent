import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type {
  AgentStepEvent,
  ArtifactUpdateEvent,
  ChatMessage,
  DurableTaskEvent,
  DurableTaskSnapshot,
  DurableTaskSubmittedEvent,
  GenerationResult,
  MultiStepInfo,
  RunCreatedEvent,
  StepUpdate,
} from "../types";
import { createId } from "../lib/createId";
import {
  durableResultHeadRevision,
  durableResultTaskStatus,
  durableChangeSetHeadRevision,
  durableSnapshotHeadRevision,
  durableSnapshotReplayCursor,
  shouldApplyDurableEvent,
} from "../adapters/durableTaskAdapter";
import type { DurableChangeSetDetail } from "../types/engineering";

export interface StepHistoryEntry extends StepUpdate {
  timestamp: number;
}

export interface DurablePanelContext {
  projectId: string | null;
  branchId: string | null;
  baseRevisionId: string | null;
  currentRevisionId: string | null;
  workflowRunId: string | null;
  changeSetId: string | null;
  lastEventSequence: number;
  taskStatus: string | null;
  preparedResult: GenerationResult | null;
  agent: DurableTaskSnapshot["agent"];
  confirmation: DurableTaskSnapshot["confirmation"];
}

export function emptyDurableContext(): DurablePanelContext {
  return {
    projectId: null,
    branchId: null,
    baseRevisionId: null,
    currentRevisionId: null,
    workflowRunId: null,
    changeSetId: null,
    lastEventSequence: 0,
    taskStatus: null,
    preparedResult: null,
    agent: null,
    confirmation: null,
  };
}

export interface ArtifactHistoryEntry extends ArtifactUpdateEvent {
  timestamp: number;
}

function hasTerminalStep(history: StepHistoryEntry[]) {
  return history.some(
    (entry) =>
      entry.step === "complete" ||
      entry.step === "cancelled" ||
      entry.step === "failed" ||
      entry.status === "failed" ||
      entry.stage_id === "design_confirmation",
  );
}

function isRunGenerating(status?: string | null) {
  return status === "running" || status === "pending";
}

function isStepGenerating(step: StepUpdate | null) {
  if (!step) return false;
  if (step.step === "resume_available" || step.step === "complete" || step.step === "failed") return false;
  return step.status === "running" || step.status === "queued";
}

function terminalStepFromResult(success: boolean, needsConfirmation = false, cancelled = false): StepHistoryEntry {
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
    step: cancelled ? "cancelled" : success ? "complete" : "failed",
    message: cancelled ? "任务已取消" : success ? "\u751f\u6210\u5b8c\u6210" : "\u751f\u6210\u5931\u8d25",
    status: cancelled ? "skipped" : success ? "success" : "failed",
    stage_id: cancelled ? "cancelled" : success ? "complete" : "failed",
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
  durable?: DurablePanelContext;
  activeRun: RunCreatedEvent | null;
  artifactUpdates: ArtifactHistoryEntry[];
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
    durable: emptyDurableContext(),
    activeRun: null,
    artifactUpdates: [],
  };
}

function normalizePersistedPanel(
  panel: Partial<PanelState>,
): PanelState {
  const fallback = createPanel(panel.title || "新对话");
  return {
    ...fallback,
    ...panel,
    messages: panel.messages || [],
    stepHistory: panel.stepHistory || [],
    activeRun: panel.activeRun || null,
    artifactUpdates: panel.artifactUpdates || [],
    durable: {
      ...emptyDurableContext(),
      ...(panel.durable || {}),
    },
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
  setRunCreated: (run: RunCreatedEvent, panelId?: string) => void;
  setDurableWorkflowStarted: (
    task: DurableTaskSubmittedEvent,
    panelId?: string,
  ) => void;
  setStep: (step: StepUpdate | null, panelId?: string) => void;
  setAgentStep: (step: AgentStepEvent, panelId?: string) => void;
  addArtifactUpdate: (artifact: ArtifactUpdateEvent, panelId?: string) => void;
  setResult: (result: GenerationResult, panelId?: string) => void;
  restorePanelResult: (panelId: string, result: GenerationResult, code: string) => void;
  setError: (error: string, panelId?: string) => void;
  applyDurableSnapshot: (snapshot: DurableTaskSnapshot, panelId?: string) => void;
  applyDurableChangeSet: (
    detail: DurableChangeSetDetail,
    panelId?: string,
  ) => void;
  applyDurableEvent: (event: DurableTaskEvent, panelId?: string) => void;
  setDurableTaskStatus: (
    status: string,
    lastEventSequence: number,
    panelId?: string,
  ) => void;
  resetDurableEventCursor: (sequence: number, panelId?: string) => void;
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
    panels: {
      id: string;
      title: string;
      messages: ChatMessage[];
      currentCode?: string | null;
      projectId?: string | null;
      branchId?: string | null;
      currentRevisionId?: string | null;
      workflowRunId?: string | null;
      workflowStatus?: string | null;
      changeSetId?: string | null;
    }[],
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
    .find(
      (message) =>
        message.result?.success
        || message.result?.needs_confirmation,
    )
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
  if (
    !latest?.result
    || latest.result.success
    || latest.result.needs_confirmation
    || [
      "pending",
      "planning",
      "running",
      "waiting_confirmation",
      "cancelling",
    ].includes(latest.result.task_status || "")
  ) {
    return null;
  }
  return latest.result.error?.message || "任务执行失败";
}

function isDurableTaskActive(status?: string | null): boolean {
  return [
    "pending",
    "planning",
    "running",
    "waiting_confirmation",
    "cancelling",
  ].includes(status || "");
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
        activeRun: null,
        artifactUpdates: [],
      })),
    })),

  setRunCreated: (run, panelId) =>
    set((state) => {
      const targetId = panelId || run.panel_id || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (panel) => {
          const generating = isRunGenerating(run.status);
          return {
            activeRun: run,
            isGenerating: generating,
            generationStartTime: generating ? (panel.generationStartTime ?? Date.now()) : null,
          };
        }),
      };
    }),

  setStep: (step, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (p) => {
          const now = Date.now();
          const generating = isStepGenerating(step);
          const base: Partial<PanelState> = {
            currentStep: step,
            isGenerating: generating,
            generationStartTime: generating ? (p.generationStartTime ?? now) : null,
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

  setAgentStep: (agentStep, panelId) => {
    const normalizedStatus =
      agentStep.status === "succeeded"
        ? "success"
        : agentStep.status === "pending"
          ? "queued"
          : agentStep.status;
    const step: StepUpdate = agentStep.legacy_step || {
      step: agentStep.step_type,
      message: agentStep.message,
      status: normalizedStatus as StepUpdate["status"],
      started_at: agentStep.started_at,
      duration_ms: agentStep.duration_ms,
      detail: agentStep.detail,
    };
    get().setStep(step, panelId || agentStep.panel_id);
  },

  setDurableWorkflowStarted: (task, panelId) =>
    set((state) => {
      const targetId = panelId || task.panel_id || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (panel) => {
          const sameWorkflow = (
            panel.durable?.workflowRunId === task.workflow_run_id
          );
          return {
            isGenerating: true,
            generationStartTime: Date.now(),
            currentStep: {
              step: "workflow.created",
              message: "持久任务已提交",
              status: "queued",
            },
            lastError: null,
            durable: {
              ...emptyDurableContext(),
              projectId: task.project_id,
              branchId: task.branch_id,
              baseRevisionId: task.expected_base_revision_id,
              currentRevisionId: task.expected_base_revision_id,
              workflowRunId: task.workflow_run_id,
              lastEventSequence: sameWorkflow
                ? panel.durable?.lastEventSequence || 0
                : 0,
              taskStatus: task.status,
              preparedResult: sameWorkflow
                ? panel.durable?.preparedResult || null
                : null,
              agent: sameWorkflow ? panel.durable?.agent || null : null,
              confirmation: sameWorkflow
                ? panel.durable?.confirmation || null
                : null,
            },
          };
        }),
      };
    }),

  addArtifactUpdate: (artifact, panelId) =>
    set((state) => {
      const targetId = panelId || artifact.panel_id || state.activePanelId;
      const entry: ArtifactHistoryEntry = { ...artifact, timestamp: Date.now() };
      return {
        panels: updatePanel(state.panels, targetId, (panel) => ({
          artifactUpdates: [...panel.artifactUpdates, entry],
        })),
      };
    }),

  setResult: (result, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (p) => {
          const previousWorkflowRunId = p.durable?.workflowRunId || null;
          const cancelled = result.task_status === "cancelled";
          const nextWorkflowRunId = result.workflow_run_id
            || previousWorkflowRunId;
          const assistantMsg: ChatMessage = {
            role: "assistant",
            content: cancelled ? "任务已取消" : result.needs_confirmation
              ? "设计简报需要确认"
              : result.success
                ? "CAD 模型已生成"
              : `生成失败: ${result.error?.message || "未知错误"}`,
            result,
          };
          const nextStepHistory = hasTerminalStep(p.stepHistory)
            ? p.stepHistory
            : [...p.stepHistory, terminalStepFromResult(Boolean(result.success), Boolean(result.needs_confirmation), cancelled)];

          return {
            result: result.success || result.needs_confirmation ? result : p.result,
            lastError: cancelled || result.success || result.needs_confirmation ? null : result.error?.message || "任务执行失败",
            isGenerating: false,
            activeRun: p.activeRun
              ? { ...p.activeRun, status: cancelled ? "cancelled" : result.success ? "succeeded" : result.needs_confirmation ? "blocked" : "failed" }
              : p.activeRun,
            currentStep: null,
            multiStepProgress: null,
            stepHistory: nextStepHistory,
            generationStartTime: null,
            messages: [...p.messages, assistantMsg],
            durable: {
              ...(p.durable || emptyDurableContext()),
              projectId: result.project_id || p.durable?.projectId || null,
              branchId: result.branch_id || p.durable?.branchId || null,
              baseRevisionId: result.expected_base_revision_id
                || p.durable?.baseRevisionId
                || null,
              currentRevisionId: durableResultHeadRevision(
                result,
                p.durable?.currentRevisionId || null,
              ),
              workflowRunId: nextWorkflowRunId,
              changeSetId: result.change_set_id
                || p.durable?.changeSetId
                || null,
              lastEventSequence: result.workflow_run_id
                && result.workflow_run_id !== previousWorkflowRunId
                ? 0
                : p.durable?.lastEventSequence || 0,
              taskStatus: durableResultTaskStatus(
                result,
                p.durable?.taskStatus || null,
              ),
            },
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
          activeRun: null,
          artifactUpdates: [],
          messages: [...panel.messages, restoreMessage],
          durable: {
            ...(panel.durable || emptyDurableContext()),
            currentRevisionId: durableResultHeadRevision(
              result,
              panel.durable?.currentRevisionId || null,
            ),
          },
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

  applyDurableSnapshot: (snapshot, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (panel) => {
          const cancelled = snapshot.status === "cancelled";
          const terminal = [
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
          ].includes(snapshot.status);
          const prepared = (
            panel.durable?.preparedResult
            || (
              typeof snapshot.request_payload.primary?.source_code
                === "string"
                ? {
                    success: false,
                    code: snapshot.request_payload.primary.source_code,
                  } as GenerationResult
                : null
            )
          );
          const files = Object.fromEntries(
            (snapshot.artifacts || []).map((artifact) => [
              artifact.artifact_kind,
              artifact.download_url,
            ]),
          );
          const snapshotResult = terminal
            ? {
                ...(prepared || { success: false }),
                request_id: snapshot.id,
                success: snapshot.status === "succeeded",
                files,
                project_id: snapshot.project_id,
                branch_id: snapshot.request_payload.branch_id,
                expected_base_revision_id:
                  snapshot.request_payload.expected_base_revision_id,
                revision_id:
                  snapshot.change_set?.candidate_revision_id,
                workflow_run_id: snapshot.id,
                change_set_id: snapshot.change_set?.id,
                task_status: snapshot.status,
                parameters: snapshot.parameters || prepared?.parameters || null,
                parameter_state_sha256:
                  snapshot.parameter_state_sha256
                  || prepared?.parameter_state_sha256
                  || null,
                error: snapshot.status === "succeeded" || cancelled
                  ? undefined
                  : {
                      type: snapshot.error?.code
                        || snapshot.error_code
                        || "WorkflowFailed",
                      message: snapshot.error?.message
                        || snapshot.error_message
                        || "持久任务执行失败",
                      details: snapshot.error
                        ? {
                            category: snapshot.error.category,
                            operation_id: snapshot.error.operation_id,
                            action: snapshot.error.action,
                            ...snapshot.error.details,
                          }
                        : undefined,
                      retryable: snapshot.error?.retryable,
                    },
              } as GenerationResult
            : panel.result;
          const terminalResult = (
            terminal
            && snapshot.status !== "succeeded"
            && panel.result?.success
          )
            ? panel.result
            : snapshotResult;
          return {
          durable: {
            ...(panel.durable || emptyDurableContext()),
            projectId: snapshot.project_id,
            branchId: snapshot.request_payload.branch_id
              || panel.durable?.branchId
              || null,
            baseRevisionId: snapshot.request_payload.expected_base_revision_id
              || snapshot.change_set?.base_revision_id
              || panel.durable?.baseRevisionId
              || null,
            currentRevisionId: durableSnapshotHeadRevision(
              snapshot,
              panel.durable?.currentRevisionId || null,
            ),
            workflowRunId: snapshot.id,
            changeSetId: snapshot.change_set?.id
              || panel.durable?.changeSetId
              || null,
            // A snapshot is metadata, not proof that preceding events were
            // consumed. Keep the actual replay cursor until each event arrives.
            lastEventSequence: durableSnapshotReplayCursor(
              panel.durable?.workflowRunId || null,
              panel.durable?.lastEventSequence || 0,
              snapshot,
            ),
            taskStatus: snapshot.status,
            agent: snapshot.agent || null,
            confirmation: snapshot.confirmation || null,
          },
          result: terminalResult,
          isGenerating: !terminal,
          lastError: terminal
            ? (
                snapshot.status === "succeeded" || cancelled
                  ? null
                  : snapshot.error?.message
                    || snapshot.error_message
                    || "持久任务执行失败"
              )
            : panel.lastError,
          };
        }),
      };
    }),

  applyDurableEvent: (event, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (panel) => {
          const durable = panel.durable || emptyDurableContext();
          if (!shouldApplyDurableEvent(durable.lastEventSequence, event)) {
            return {};
          }
          const preparedPayload = event.event_type === "source.prepared"
            ? {
                success: false,
                needs_confirmation:
                  event.payload.needs_confirmation === true,
                code: typeof event.payload.source_code === "string"
                  ? event.payload.source_code
                  : undefined,
                plan: typeof event.payload.plan === "object"
                  ? event.payload.plan
                  : undefined,
                design_brief:
                  typeof event.payload.design_brief === "object"
                    ? event.payload.design_brief
                    : undefined,
                manufacturing_profile:
                  typeof event.payload.manufacturing_profile === "object"
                    ? event.payload.manufacturing_profile
                    : undefined,
                workflow_run_id: event.workflow_run_id,
                project_id: durable.projectId || undefined,
                branch_id: durable.branchId || undefined,
                expected_base_revision_id:
                  durable.baseRevisionId || undefined,
              } as GenerationResult
            : null;
          return {
            durable: {
              ...durable,
              workflowRunId: event.workflow_run_id,
              lastEventSequence: event.sequence,
              preparedResult:
                preparedPayload || durable.preparedResult,
              agent: durable.agent,
            },
            result: preparedPayload?.needs_confirmation
              ? preparedPayload
              : panel.result,
            isGenerating: preparedPayload?.needs_confirmation
              ? false
              : panel.isGenerating,
          };
        }),
      };
    }),

  applyDurableChangeSet: (detail, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (panel) => ({
          durable: {
            ...(panel.durable || emptyDurableContext()),
            projectId: detail.project_id,
            branchId: detail.branch_id,
            baseRevisionId: detail.base_revision_id,
            currentRevisionId: durableChangeSetHeadRevision(detail),
            workflowRunId: detail.source_workflow_run_id
              || panel.durable?.workflowRunId
              || null,
            changeSetId: detail.id,
            taskStatus: detail.workflow_status
              || panel.durable?.taskStatus
              || null,
          },
        })),
      };
    }),

  setDurableTaskStatus: (status, lastEventSequence, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (panel) => ({
          durable: {
            ...(panel.durable || emptyDurableContext()),
            lastEventSequence: Math.max(
              lastEventSequence,
              panel.durable?.lastEventSequence || 0,
            ),
            taskStatus: status,
          },
          isGenerating: ![
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
          ].includes(status),
        })),
      };
    }),

  resetDurableEventCursor: (sequence, panelId) =>
    set((state) => {
      const targetId = panelId || state.activePanelId;
      return {
        panels: updatePanel(state.panels, targetId, (panel) => ({
          durable: {
            ...(panel.durable || emptyDurableContext()),
            lastEventSequence: Math.max(0, sequence),
          },
        })),
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
        activeRun: null,
        artifactUpdates: [],
        baselineVersion: 1,
      })),
    })),

  loadSession: (sessionId, panels) => {
    const panelStates = panels.map((p) => {
      const result = restoreGenerationResult(
        p.messages,
        p.currentCode,
      );
      const workflowStatus = (
        p.workflowStatus
        || result?.task_status
        || null
      );
      return {
        ...createPanel(p.title),
        id: p.id,
        messages: p.messages,
        result,
        isGenerating: isDurableTaskActive(workflowStatus),
        generationStartTime: isDurableTaskActive(workflowStatus)
          ? Date.now()
          : null,
        lastError: restoreLastError(p.messages),
        activeRun: null,
        artifactUpdates: [],
        baselineVersion: 1,
        durable: {
          ...emptyDurableContext(),
          projectId: p.projectId || null,
          branchId: p.branchId || null,
          baseRevisionId: p.currentRevisionId || null,
          currentRevisionId: p.currentRevisionId || null,
          workflowRunId: (
            p.workflowRunId
            || result?.workflow_run_id
            || null
          ),
          changeSetId: p.changeSetId || result?.change_set_id || null,
          taskStatus: workflowStatus,
          preparedResult: result,
        },
      };
    });
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
  merge: (persistedState, currentState) => {
    const saved = persistedState as Partial<SessionState>;
    const panels = Array.isArray(saved.panels) && saved.panels.length > 0
      ? saved.panels.map((panel) => normalizePersistedPanel(panel))
      : currentState.panels;
    const requestedActivePanelId = saved.activePanelId;
    const activePanelId = panels.some(
      (panel) => panel.id === requestedActivePanelId,
    )
      ? requestedActivePanelId as string
      : panels[0].id;
    return {
      ...currentState,
      ...saved,
      panels,
      activePanelId,
    };
  },
}));
