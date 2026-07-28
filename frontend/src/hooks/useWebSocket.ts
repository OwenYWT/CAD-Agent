import { useCallback, useEffect, useRef, useState } from "react";
import { getAuthToken } from "../auth";
import { useSessionStore } from "../stores/sessionStore";
import type { CapabilitySelection, ManufacturingProfile, WSMessage } from "../types";

const MAX_RECONNECT_ATTEMPTS = 5;
const BASE_RECONNECT_DELAY_MS = 1000;

export type ConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected";

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { sessionId, setStep, setResult, setError } = useSessionStore();
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
        for (const panel of state.panels) {
          if (panel.isGenerating) {
            ws.send(JSON.stringify({
              type: "restore_task",
              panel_id: panel.id,
            }));
          }
        }
        reconnectAttemptRef.current = 0;
      };

      ws.onmessage = (event) => {
        try {
          const msg: WSMessage = JSON.parse(event.data);
          if (msg.type === "step_update") {
            setStep(msg.data, msg.data.panel_id);
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
          setError("连接已断开，请刷新页面后重试");
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
  }, [sessionId, setStep, setResult, setError]);

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
        content: `修改零件 ${partName}: ${instruction}`,
      });
      wsRef.current.send(JSON.stringify({
        type: "modify_part",
        part_name: partName,
        instruction,
        panel_id: panelId,
      }));
    }
  }, []);

  const restoreContext = useCallback((panelId: string, code: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "restore_context", panel_id: panelId, code }));
    }
  }, []);

  return {
    connectionState,
    sendMessage,
    executeCode,
    cancelGeneration,
    modifyPart,
    restoreContext,
  };
}
