import { useCallback, useEffect, useRef, useState } from "react";
import { getAuthToken } from "../auth";
import {
  durableEventStep,
  durableWriteIdentity,
  shouldApplyDurableEvent,
  webSocketAuthProtocol,
} from "../adapters/durableTaskAdapter";
import { createId } from "../lib/createId";
import {
  emptyDurableContext,
  useSessionStore,
} from "../stores/sessionStore";
import type {
  AssemblyPartInfo,
  CapabilitySelection,
  DurableWSMessage,
  ManufacturingProfile,
  WSMessage,
} from "../types";
import type { PanelState } from "../stores/sessionStore";

const MAX_RECONNECT_ATTEMPTS = 5;
const BASE_RECONNECT_DELAY_MS = 1000;

export type ConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected";

export function durableIdentityPayload(panel: PanelState) {
  const durable = panel.durable || emptyDurableContext();
  return durableWriteIdentity(durable);
}

export function operationIntentForPanel(
  panel: PanelState | undefined,
): "generate" | "modify" {
  return panel?.result?.success ? "modify" : "generate";
}

function requestPanelReplay(ws: WebSocket) {
  const state = useSessionStore.getState();
  const panelIds = new Set<string>();
  if (state.activePanelId) {
    panelIds.add(state.activePanelId);
  }
  for (const panel of state.panels) {
    panelIds.add(panel.id);
  }

  for (const panelId of panelIds) {
    const panel = state.panels.find((item) => item.id === panelId);
    ws.send(JSON.stringify({
      type: "restore_context",
      panel_id: panelId,
      code: panel?.result?.code || "",
      assembly_parts: panel?.result?.assembly_parts || [],
    }));
  }
}

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const durableWsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const {
    sessionId,
    setRunCreated,
    setDurableWorkflowStarted,
    setStep,
    setAgentStep,
    addArtifactUpdate,
    setResult,
    setError,
    applyDurableSnapshot,
    applyDurableEvent,
    setDurableTaskStatus,
    resetDurableEventCursor,
  } = useSessionStore();
  const activePanelId = useSessionStore((state) => state.activePanelId);
  const durableWorkflowRunId = useSessionStore((state) => (
    state.panels.find((panel) => panel.id === state.activePanelId)
      ?.durable?.workflowRunId || null
  ));
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");

  useEffect(() => {
    let disposed = false;
    let startupTimer: ReturnType<typeof setTimeout> | null = null;

    const openSocket = () => {
      if (disposed) return;
      setConnectionState(reconnectAttemptRef.current > 0 ? "reconnecting" : "connecting");

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const apiBase = import.meta.env.VITE_API_BASE || window.location.host;
      const host = apiBase.replace(/^https?:\/\//, "");
      const token = getAuthToken() || import.meta.env.VITE_API_TOKEN;
      const tokenParam = token ? `?token=${encodeURIComponent(token)}` : "";
      const ws = new WebSocket(`${protocol}//${host}/ws/${sessionId}${tokenParam}`);
      wsRef.current = ws;

      ws.onopen = () => {
        if (disposed) return;
        setConnectionState("connected");
        const state = useSessionStore.getState();
        requestPanelReplay(ws);
        for (const panel of state.panels) {
          if (panel.isGenerating) {
            ws.send(JSON.stringify({
              type: "restore_task",
              panel_id: panel.id,
              workflow_run_id: panel.durable?.workflowRunId,
            }));
          }
        }
        reconnectAttemptRef.current = 0;
      };

      ws.onmessage = (event) => {
        try {
          const msg: WSMessage = JSON.parse(event.data);
          if (msg.type === "run_created") {
            setRunCreated(msg.data, msg.data.panel_id);
          } else if (msg.type === "task_submitted") {
            setDurableWorkflowStarted(msg.data, msg.data.panel_id);
          } else if (msg.type === "step_update") {
            setStep(msg.data, msg.data.panel_id);
          } else if (msg.type === "agent_step") {
            setAgentStep(msg.data, msg.data.panel_id);
          } else if (msg.type === "artifact_update") {
            addArtifactUpdate(msg.data, msg.data.panel_id);
          } else if (msg.type === "generation_result") {
            setResult(msg.data, msg.data.panel_id);
          } else if (msg.type === "task_status" && msg.data.status === "not_found") {
            setError("未找到可恢复的任务，请重新提交", msg.data.panel_id);
          }
        } catch {
          console.error("Failed to parse WebSocket message");
        }
      };

      ws.onerror = () => {};

      ws.onclose = (event) => {
        if (disposed || wsRef.current !== ws) return;
        wsRef.current = null;

        if (event.code === 1000) {
          setConnectionState("disconnected");
          return;
        }

        if (reconnectAttemptRef.current < MAX_RECONNECT_ATTEMPTS) {
          const attempt = reconnectAttemptRef.current;
          const delay = BASE_RECONNECT_DELAY_MS * 2 ** attempt + Math.random() * BASE_RECONNECT_DELAY_MS;
          reconnectAttemptRef.current = attempt + 1;
          setConnectionState("reconnecting");
          reconnectTimerRef.current = setTimeout(openSocket, delay);
        } else {
          setConnectionState("disconnected");
          setError("\u8fde\u63a5\u5df2\u65ad\u5f00\uff0c\u8bf7\u5237\u65b0\u9875\u9762\u540e\u91cd\u8bd5");
        }
      };
    };

    reconnectAttemptRef.current = 0;
    // React StrictMode mounts, cleans up, then mounts effects again in development.
    // Defer the initial socket by one task so the throwaway effect is cancelled
    // before it creates a CONNECTING socket that Chrome reports as a warning.
    startupTimer = setTimeout(openSocket, 0);

    return () => {
      disposed = true;
      if (startupTimer) {
        clearTimeout(startupTimer);
        startupTimer = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close(1000);
    };
  }, [sessionId, setRunCreated, setDurableWorkflowStarted, setStep, setAgentStep, addArtifactUpdate, setResult, setError]);

  useEffect(() => {
    if (!durableWorkflowRunId) return;
    let disposed = false;
    let reconnectAttempts = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const openDurableSocket = () => {
      if (disposed) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const apiBase = import.meta.env.VITE_API_BASE || window.location.host;
      const host = apiBase.replace(/^https?:\/\//, "");
      const token = getAuthToken() || import.meta.env.VITE_API_TOKEN;
      const panel = useSessionStore.getState().panels.find(
        (candidate) => candidate.id === activePanelId,
      );
      const cursor = panel?.durable?.lastEventSequence || 0;
      const query = new URLSearchParams({ after_sequence: String(cursor) });
      const authProtocol = webSocketAuthProtocol(token);
      const socket = new WebSocket(
        `${protocol}//${host}/ws/tasks/${durableWorkflowRunId}?${query}`,
        authProtocol ? [authProtocol] : undefined,
      );
      durableWsRef.current = socket;

      socket.onopen = () => {
        reconnectAttempts = 0;
      };
      socket.onmessage = (raw) => {
        try {
          const message: DurableWSMessage = JSON.parse(raw.data);
          if (message.type === "task_snapshot") {
            applyDurableSnapshot(message.data, activePanelId);
            return;
          }
          if (message.type === "task_event") {
            const current = useSessionStore.getState().panels.find(
              (candidate) => candidate.id === activePanelId,
            )?.durable?.lastEventSequence || 0;
            if (!shouldApplyDurableEvent(current, message.data)) return;
            applyDurableEvent(message.data, activePanelId);
            setStep(durableEventStep(message.data), activePanelId);
            return;
          }
          if (message.type === "task_stream_complete") {
            setDurableTaskStatus(
              message.data.status,
              message.data.last_event_sequence,
              activePanelId,
            );
            setStep(null, activePanelId);
            return;
          }
          if (message.type === "cursor_expired") {
            resetDurableEventCursor(
              message.data.earliest_sequence - 1,
              activePanelId,
            );
          }
        } catch {
          setError("持久任务事件解析失败", activePanelId);
        }
      };
      socket.onclose = (event) => {
        if (disposed || durableWsRef.current !== socket) return;
        durableWsRef.current = null;
        if (event.code === 1000) return;
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
          setError("持久任务事件连接已断开，请刷新后重试", activePanelId);
          return;
        }
        const delay = BASE_RECONNECT_DELAY_MS * 2 ** reconnectAttempts;
        reconnectAttempts += 1;
        reconnectTimer = setTimeout(openDurableSocket, delay);
      };
    };

    openDurableSocket();
    return () => {
      disposed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      const socket = durableWsRef.current;
      durableWsRef.current = null;
      socket?.close(1000);
    };
  }, [
    activePanelId,
    applyDurableEvent,
    applyDurableSnapshot,
    durableWorkflowRunId,
    resetDurableEventCursor,
    setDurableTaskStatus,
    setError,
    setStep,
  ]);

  const sendMessage = useCallback((
    text: string,
    capability: CapabilitySelection = "auto",
    manufacturingProfile: ManufacturingProfile | null = null,
  ) => {
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return false;
    const state = useSessionStore.getState();
    const panelId = state.activePanelId;
    const panel = state.panels.find((candidate) => candidate.id === panelId);
    const identity = panel ? durableIdentityPayload(panel) : {};
    ws.send(JSON.stringify({
      type: "user_message",
      text,
      operation_intent: operationIntentForPanel(panel),
      capability,
      panel_id: panelId,
      workflow_run_id: panel?.durable?.workflowRunId || undefined,
      manufacturing_profile: manufacturingProfile,
      ...identity,
      idempotency_key: identity.idempotency_key || createId(),
    }));
    return true;
  }, []);

  const executeCode = useCallback((code: string) => {
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return false;
    const state = useSessionStore.getState();
    const panelId = state.activePanelId;
    const panel = state.panels.find((candidate) => candidate.id === panelId);
    const identity = panel ? durableIdentityPayload(panel) : {};
    ws.send(JSON.stringify({
      type: "execute_code",
      code,
      panel_id: panelId,
      ...identity,
      idempotency_key: identity.idempotency_key || createId(),
    }));
    return true;
  }, []);

  const restoreRevision = useCallback((revisionId: string) => {
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return false;
    const state = useSessionStore.getState();
    const panel = state.panels.find((candidate) => candidate.id === state.activePanelId);
    if (!panel || panel.isGenerating || !revisionId) return false;
    const identity = durableIdentityPayload(panel);
    if (!identity.project_id) return false;
    ws.send(JSON.stringify({
      type: "restore_revision",
      source_revision_id: revisionId,
      panel_id: panel.id,
      ...identity,
    }));
    return true;
  }, []);

  const resumeRun = useCallback((runId: string) => {
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return false;
    const state = useSessionStore.getState();
    const panelId = state.activePanelId;
    const panel = state.panels.find(
      (candidate) => candidate.id === panelId,
    );
    const identity = panel ? durableIdentityPayload(panel) : {};
    ws.send(JSON.stringify({
      type: "resume_run",
      run_id: runId,
      panel_id: panelId,
      ...identity,
      idempotency_key: identity.idempotency_key || createId(),
    }));
    return true;
  }, []);

  const cancelGeneration = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      const panelId = useSessionStore.getState().activePanelId;
      const workflowRunId = useSessionStore.getState().panels.find(
        (panel) => panel.id === panelId,
      )?.durable?.workflowRunId;
      wsRef.current.send(JSON.stringify({
        type: "cancel",
        panel_id: panelId,
        workflow_run_id: workflowRunId,
      }));
    }
  }, []);

  const modifyPart = useCallback((partName: string, instruction: string) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
    const panelId = useSessionStore.getState().activePanelId;
    const panel = useSessionStore.getState().panels.find(
      (candidate) => candidate.id === panelId,
    );
    const identity = panel ? durableIdentityPayload(panel) : {};
    useSessionStore.getState().addMessage({
      role: "user",
      content: `\u4fee\u6539\u96f6\u4ef6 ${partName}: ${instruction}`,
    });
    wsRef.current.send(JSON.stringify({
      type: "modify_part",
      part_name: partName,
      instruction,
      panel_id: panelId,
      code: panel?.result?.code || undefined,
      ...identity,
      idempotency_key: identity.idempotency_key || createId(),
    }));
    return true;
  }, []);

  const modifyParameters = useCallback((
    updates: { parameter_id: string; value: number }[],
    expectedStateSha256?: string | null,
  ) => {
    if (wsRef.current?.readyState !== WebSocket.OPEN) return false;
    const state = useSessionStore.getState();
    const panelId = state.activePanelId;
    const panel = state.panels.find((candidate) => candidate.id === panelId);
    const stateSha256 = expectedStateSha256
      || panel?.result?.parameter_state_sha256;
    if (!panel || !stateSha256 || updates.length === 0) return false;
    const identity = durableIdentityPayload(panel);
    if (!identity.project_id) return false;
    wsRef.current.send(JSON.stringify({
      type: "modify_parameters",
      updates,
      expected_state_sha256: stateSha256,
      panel_id: panelId,
      ...identity,
      idempotency_key: identity.idempotency_key || createId(),
    }));
    return true;
  }, []);

  const restoreContext = useCallback((panelId: string, code = "", assemblyParts: AssemblyPartInfo[] = []) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "restore_context", panel_id: panelId, code, assembly_parts: assemblyParts }));
    }
  }, []);

  return {
    connectionState,
    sendMessage,
    executeCode,
    restoreRevision,
    resumeRun,
    cancelGeneration,
    modifyPart,
    modifyParameters,
    restoreContext,
  };
}
