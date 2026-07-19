import { useEffect, useRef, useCallback } from "react";
import { useSessionStore } from "../stores/sessionStore";
import type { ManufacturingProfile } from "../types";
import type { WSMessage } from "../types";
import { getAuthToken } from "../auth";

const MAX_RECONNECT_ATTEMPTS = 5;
const BASE_RECONNECT_DELAY_MS = 1000;

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { sessionId, setStep, setResult, setError } = useSessionStore();

  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const apiBase = import.meta.env.VITE_API_BASE || window.location.host;
    const host = apiBase.replace(/^https?:\/\//, "");
    const token = getAuthToken() || import.meta.env.VITE_API_TOKEN;
    const tokenParam = token ? `?token=${encodeURIComponent(token)}` : "";
    const url = `${protocol}//${host}/ws/${sessionId}${tokenParam}`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      console.log("WebSocket connected");
      // If this is a RECONNECT (not the first connect), any generation that was
      // in flight on the dropped socket is lost — the backend created a fresh
      // context and will never send its result. Without this, isGenerating stays
      // true forever and the input is permanently disabled. Unstick every panel
      // that was mid-generation so the tester can retry instead of refreshing.
      if (reconnectAttemptRef.current > 0) {
        const state = useSessionStore.getState();
        for (const p of state.panels) {
          if (p.isGenerating) {
            state.setError("连接中断，本次生成已丢失，请重试", p.id);
          }
        }
      }
      reconnectAttemptRef.current = 0;
    };

    ws.onmessage = (event) => {
      try {
        const msg: WSMessage = JSON.parse(event.data);
        switch (msg.type) {
          case "step_update": {
            const panelId = msg.data.panel_id;
            setStep(msg.data, panelId);
            break;
          }
          case "generation_result": {
            const panelId = msg.data.panel_id;
            setResult(msg.data, panelId);
            break;
          }
          case "assistant_message":
            break;
        }
      } catch {
        console.error("Failed to parse WS message");
      }
    };

    ws.onerror = () => {};

    ws.onclose = (event) => {
      if (wsRef.current !== ws) return;
      wsRef.current = null;

      if (event.code === 1000) return;

      if (reconnectAttemptRef.current < MAX_RECONNECT_ATTEMPTS) {
        const attempt = reconnectAttemptRef.current;
        const delay =
          BASE_RECONNECT_DELAY_MS * Math.pow(2, attempt) +
          Math.random() * BASE_RECONNECT_DELAY_MS;
        console.log(
          `WebSocket reconnecting in ${Math.round(delay)}ms (attempt ${attempt + 1}/${MAX_RECONNECT_ATTEMPTS})`,
        );
        reconnectAttemptRef.current = attempt + 1;
        reconnectTimerRef.current = setTimeout(connect, delay);
      } else {
        setError("WebSocket 连接丢失，请刷新页面重试");
      }
    };

    return ws;
  }, [sessionId, setStep, setResult, setError]);

  useEffect(() => {
    reconnectAttemptRef.current = 0;
    const ws = connect();

    return () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      ws.close(1000);
    };
  }, [connect]);

  const sendMessage = useCallback((text: string, manufacturingProfile?: ManufacturingProfile | null) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      const panelId = useSessionStore.getState().activePanelId;
      ws.send(JSON.stringify({
        type: "user_message",
        text,
        panel_id: panelId,
        manufacturing_profile: manufacturingProfile || null,
      }));
    } else {
      console.warn("WebSocket not open, readyState:", ws?.readyState);
      useSessionStore.getState().setError("连接未就绪，请稍后重试");
    }
  }, []);

  const executeCode = useCallback((code: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      const panelId = useSessionStore.getState().activePanelId;
      wsRef.current.send(
        JSON.stringify({ type: "execute_code", code, panel_id: panelId }),
      );
    }
  }, []);

  const cancelGeneration = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      const panelId = useSessionStore.getState().activePanelId;
      wsRef.current.send(
        JSON.stringify({ type: "cancel", panel_id: panelId }),
      );
    }
  }, []);

  const modifyPart = useCallback((partName: string, instruction: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      const panelId = useSessionStore.getState().activePanelId;
      useSessionStore.getState().addMessage({
        role: "user",
        content: `修改零件 ${partName}: ${instruction}`,
      });
      wsRef.current.send(
        JSON.stringify({
          type: "modify_part",
          part_name: partName,
          instruction,
          panel_id: panelId,
        }),
      );
    }
  }, []);

  const restoreContext = useCallback((panelId: string, code: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        JSON.stringify({ type: "restore_context", panel_id: panelId, code }),
      );
    }
  }, []);

  return { sendMessage, executeCode, cancelGeneration, modifyPart, restoreContext };
}
