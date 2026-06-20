import { useState, useEffect, useCallback } from "react";
import { useSessionStore } from "../stores/sessionStore";
import { useWebSocket } from "../hooks/useWebSocket";
import type { GenerationResult } from "../types";

interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

interface PanelSummary {
  id: string;
  title: string;
  current_code: string | null;
}

interface MessageData {
  role: "user" | "assistant";
  content: string;
  result?: Record<string, unknown>;
}

const API_BASE = import.meta.env.VITE_API_BASE || "";

export default function HistorySidebar() {
  const [isOpen, setIsOpen] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const loadSession = useSessionStore((s) => s.loadSession);
  const { restoreContext } = useWebSocket();

  const fetchSessions = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/history/sessions`);
      if (res.ok) {
        setSessions(await res.json());
      }
    } catch (e) {
      console.error("Failed to fetch sessions:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) fetchSessions();
  }, [isOpen, fetchSessions]);

  const handleRestore = async (sessionId: string) => {
    try {
      const panelsRes = await fetch(
        `${API_BASE}/api/history/sessions/${sessionId}/panels`,
      );
      if (!panelsRes.ok) return;
      const panels: PanelSummary[] = await panelsRes.json();

      const panelData = await Promise.all(
        panels.map(async (p) => {
          const msgsRes = await fetch(
            `${API_BASE}/api/history/panels/${p.id}/messages`,
          );
          const messages: MessageData[] = msgsRes.ok ? await msgsRes.json() : [];
          return {
            id: p.id,
            title: p.title || "面板",
            messages: messages.map((m) => ({
              role: m.role as "user" | "assistant",
              content: m.content,
              result: m.result as unknown as GenerationResult | undefined,
            })),
            currentCode: p.current_code,
          };
        }),
      );

      loadSession(sessionId, panelData);
      // Restore backend context for panels with existing code
      for (const p of panelData) {
        if (p.currentCode) {
          restoreContext(p.id, p.currentCode);
        }
      }
      setIsOpen(false);
    } catch (e) {
      console.error("Failed to restore session:", e);
    }
  };

  const handleDelete = async (sessionId: string) => {
    try {
      await fetch(`${API_BASE}/api/history/sessions/${sessionId}`, {
        method: "DELETE",
      });
      setSessions((prev) => prev.filter((s) => s.id !== sessionId));
    } catch (e) {
      console.error("Failed to delete session:", e);
    }
  };

  const formatTime = (iso: string) => {
    try {
      const d = new Date(iso);
      return d.toLocaleDateString("zh-CN") + " " + d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    } catch {
      return iso;
    }
  };

  return (
    <>
      <button
        className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded transition-colors"
        onClick={() => setIsOpen(!isOpen)}
        title="历史记录"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      </button>

      {isOpen && (
        <div className="absolute top-12 left-0 w-72 max-h-[70vh] bg-white border border-gray-200 rounded-lg shadow-lg z-50 overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b border-gray-100 flex items-center justify-between">
            <span className="text-sm font-medium text-gray-700">历史记录</span>
            <button
              className="text-xs text-gray-400 hover:text-gray-600"
              onClick={() => setIsOpen(false)}
            >
              x
            </button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="p-4 text-center text-xs text-gray-400">
                加载中...
              </div>
            ) : sessions.length === 0 ? (
              <div className="p-4 text-center text-xs text-gray-400">
                暂无历史记录
              </div>
            ) : (
              sessions.map((s) => (
                <div
                  key={s.id}
                  className="px-3 py-2 border-b border-gray-50 hover:bg-gray-50 group"
                >
                  <div className="flex items-center justify-between">
                    <button
                      className="flex-1 text-left text-xs text-gray-700 truncate hover:text-indigo-600"
                      onClick={() => handleRestore(s.id)}
                    >
                      {s.title || s.id.slice(0, 8)}
                    </button>
                    <button
                      className="opacity-0 group-hover:opacity-60 text-xs text-red-400 hover:text-red-600 ml-2"
                      onClick={() => handleDelete(s.id)}
                      title="删除"
                    >
                      x
                    </button>
                  </div>
                  <div className="text-[10px] text-gray-400 mt-0.5">
                    {formatTime(s.updated_at)}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </>
  );
}
