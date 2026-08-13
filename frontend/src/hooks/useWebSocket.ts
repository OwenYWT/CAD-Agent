import { useCallback, useEffect, useRef, useState } from "react";
import { getAuthToken } from "../auth";
import { useSessionStore } from "../stores/sessionStore";
import type { AssemblyPartInfo, CapabilitySelection, ManufacturingProfile, WSMessage } from "../types";

const MAX_RECONNECT_ATTEMPTS = 5;
const BASE_RECONNECT_DELAY_MS = 1000;

export type ConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected";

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
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { sessionId, setRunCreated, setStep, setAgentStep, addArtifactUpdate, setResult, setError } = useSessionStore();
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");

  useEffect(() => {
    let disposed = false;

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
        requestPanelReplay(ws);
        if (reconnectAttemptRef.current > 0) {
          const state = useSessionStore.getState();
          for (const panel of state.panels) {
            if (panel.isGenerating) {
              state.setError("\u8fde\u63a5\u4e2d\u65ad\uff0c\u672c\u6b21\u751f\u6210\u5df2\u6682\u505c\uff0c\u8bf7\u67e5\u770b\u662f\u5426\u53ef\u4ee5\u7ee7\u7eed\u4e0a\u6b21\u4efb\u52a1", panel.id);
            }
          }
        }
        reconnectAttemptRef.current = 0;
      };

      ws.onmessage = (event) => {
        try {
          const msg: WSMessage = JSON.parse(event.data);
          if (msg.type === "run_created") {
            setRunCreated(msg.data, msg.data.panel_id);
          } else if (msg.type === "step_update") {
            setStep(msg.data, msg.data.panel_id);
          } else if (msg.type === "agent_step") {
            setAgentStep(msg.data, msg.data.panel_id);
          } else if (msg.type === "artifact_update") {
            addArtifactUpdate(msg.data, msg.data.panel_id);
          } else if (msg.type === "generation_result") {
            setResult(msg.data, msg.data.panel_id);
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
    openSocket();

    return () => {
      disposed = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close(1000);
    };
  }, [sessionId, setRunCreated, setStep, setAgentStep, addArtifactUpdate, setResult, setError]);

  const sendMessage = useCallback((
    text: string,
    capability: CapabilitySelection = "auto",
    manufacturingProfile: ManufacturingProfile | null = null,
  ) => {
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return false;
    const panelId = useSessionStore.getState().activePanelId;
    ws.send(JSON.stringify({
      type: "user_message",
      text,
      capability,
      panel_id: panelId,
      manufacturing_profile: manufacturingProfile,
    }));
    return true;
  }, []);

  const executeCode = useCallback((code: string) => {
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return false;
    const panelId = useSessionStore.getState().activePanelId;
    ws.send(JSON.stringify({ type: "execute_code", code, panel_id: panelId }));
    return true;
  }, []);

  const resumeRun = useCallback((runId: string) => {
    const ws = wsRef.current;
    if (ws?.readyState !== WebSocket.OPEN) return false;
    const panelId = useSessionStore.getState().activePanelId;
    ws.send(JSON.stringify({ type: "resume_run", run_id: runId, panel_id: panelId }));
    return true;
  }, []);

  const cancelGeneration = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      const panelId = useSessionStore.getState().activePanelId;
      wsRef.current.send(JSON.stringify({ type: "cancel", panel_id: panelId }));
    }
  }, []);

  const modifyPart = useCallback((partName: string, instruction: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      const panelId = useSessionStore.getState().activePanelId;
      useSessionStore.getState().addMessage({
        role: "user",
        content: `\u4fee\u6539\u96f6\u4ef6 ${partName}: ${instruction}`,
      });
      wsRef.current.send(JSON.stringify({
        type: "modify_part",
        part_name: partName,
        instruction,
        panel_id: panelId,
      }));
    }
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
    resumeRun,
    cancelGeneration,
    modifyPart,
    restoreContext,
  };
}