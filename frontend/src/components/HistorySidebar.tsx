import { useState, useCallback } from "react";
import { useSessionStore } from "../stores/sessionStore";
import type { GenerationResult } from "../types";
import { Icon } from "./ui/Icon";

import { authFetch } from "../auth";
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

export default function HistorySidebar({ restoreContext }: { restoreContext: (panelId: string, code: string) => void }) {
  const [isOpen, setIsOpen] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadSession = useSessionStore((s) => s.loadSession);

  const fetchSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await authFetch(`${API_BASE}/api/history/sessions`);
      if (!res.ok) throw new Error("历史记录加载失败");
      setSessions(await res.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "历史记录加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const toggleOpen = () => {
    const nextOpen = !isOpen;
    setIsOpen(nextOpen);
    if (nextOpen) void fetchSessions();
  };

  const handleRestore = async (sessionId: string) => {
    try {
      const panelsRes = await authFetch(
        `${API_BASE}/api/history/sessions/${sessionId}/panels`,
      );
      if (!panelsRes.ok) return;
      const panels: PanelSummary[] = await panelsRes.json();

      const panelData = await Promise.all(
        panels.map(async (p) => {
          const msgsRes = await authFetch(
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
      setError(e instanceof Error ? e.message : "历史记录恢复失败");
    }
  };

  const handleDelete = async (sessionId: string) => {
    if (!window.confirm("确认删除这条历史记录？此操作不可恢复。")) return;
    try {
      const res = await authFetch(`${API_BASE}/api/history/sessions/${sessionId}`, {
        method: "DELETE",
      });
      if (!res.ok) throw new Error("删除失败，请重试");
      setSessions((prev) => prev.filter((s) => s.id !== sessionId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "删除失败，请重试");
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
    <div className="relative">
      <button
        aria-label="打开历史记录"
        className="icon-button text-slate-500 hover:text-slate-900 hover:bg-slate-100 transition-colors"
        onClick={toggleOpen}
        title="历史记录"
        type="button"
      >
        <Icon name="history" size={18} />
      </button>

      {isOpen && (
        <div className="fixed left-3 right-3 top-16 z-50 flex max-h-[70vh] flex-col overflow-hidden rounded-md border border-slate-200 bg-white shadow-xl sm:absolute sm:left-auto sm:right-0 sm:top-11 sm:w-80">
          <div className="px-3 py-2 border-b border-gray-100 flex items-center justify-between">
            <span className="text-sm font-medium text-gray-700">历史记录</span>
            <button
              aria-label="关闭历史记录"
              className="icon-button text-slate-500 hover:bg-slate-100 hover:text-slate-900"
              onClick={() => setIsOpen(false)}
              title="关闭"
              type="button"
            >
              <Icon name="x" size={17} />
            </button>
          </div>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="p-4 text-center text-xs text-gray-400">
                加载中...
              </div>
            ) : error ? (
              <div className="p-4 text-center text-xs text-red-600" role="alert">
                <p>{error}</p>
                <button className="mt-2 min-h-9 rounded-md border border-red-200 bg-white px-3 font-medium" onClick={() => void fetchSessions()} type="button">重试</button>
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
                      className="min-h-10 flex-1 truncate text-left text-xs text-slate-700 hover:text-sky-700"
                      onClick={() => handleRestore(s.id)}
                    >
                      {s.title || s.id.slice(0, 8)}
                    </button>
                    <button
                      aria-label={`删除历史记录 ${s.title || s.id.slice(0, 8)}`}
                      className="icon-button ml-2 text-red-400 opacity-70 hover:bg-red-50 hover:text-red-600 sm:opacity-0 sm:group-hover:opacity-100"
                      onClick={() => handleDelete(s.id)}
                      title="删除"
                      type="button"
                    >
                      <Icon name="trash" size={16} />
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
    </div>
  );
}
