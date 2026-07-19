import { useCallback, useEffect, useState } from "react";
import { authFetch } from "../auth";
import type { ModelSnapshotDetail, ModelSnapshotSummary } from "../types";

const API_BASE = import.meta.env.VITE_API_BASE || "";

interface VersionHistoryPanelProps {
  panelId: string;
  activeSnapshotId?: string | null;
  refreshKey?: string | number | null;
  onRestore: (snapshot: ModelSnapshotDetail) => void;
}

const STATUS_CLASS: Record<string, string> = {
  pass: "border-emerald-200 bg-emerald-50 text-emerald-700",
  warn: "border-amber-200 bg-amber-50 text-amber-700",
  fail: "border-red-200 bg-red-50 text-red-700",
  unknown: "border-gray-200 bg-gray-50 text-gray-600",
};

function formatTime(iso: string) {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function VersionHistoryPanel({
  panelId,
  activeSnapshotId,
  refreshKey,
  onRestore,
}: VersionHistoryPanelProps) {
  const [snapshots, setSnapshots] = useState<ModelSnapshotSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSnapshots = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await authFetch(
        `${API_BASE}/api/history/panels/${panelId}/snapshots`,
      );
      if (!response.ok) throw new Error("\u7248\u672c\u5217\u8868\u52a0\u8f7d\u5931\u8d25");
      setSnapshots(await response.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "\u7248\u672c\u5217\u8868\u52a0\u8f7d\u5931\u8d25");
    } finally {
      setLoading(false);
    }
  }, [panelId]);

  useEffect(() => {
    loadSnapshots();
  }, [loadSnapshots, refreshKey]);

  const restoreSnapshot = async (snapshotId: string) => {
    setRestoringId(snapshotId);
    setError(null);
    try {
      const response = await authFetch(
        `${API_BASE}/api/history/snapshots/${snapshotId}/restore`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error("\u672a\u8bb0\u5f55\u63d0\u793a\u8bcd");
      onRestore(await response.json());
      await loadSnapshots();
    } catch (err) {
      setError(err instanceof Error ? err.message : "\u672a\u8bb0\u5f55\u63d0\u793a\u8bcd");
    } finally {
      setRestoringId(null);
    }
  };

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">{"\u7248\u672c\u5386\u53f2"}</h3>
          <p className="text-xs text-gray-500">
            {"\u65e0\u9700\u518d\u6b21\u8c03\u7528 LLM\uff0c\u5373\u53ef\u6062\u590d\u5df2\u4fdd\u5b58\u7684 CAD \u68c0\u67e5\u70b9\u3002"}
          </p>
        </div>
        <button
          className="text-xs text-indigo-600 hover:text-indigo-700 disabled:text-gray-400"
          onClick={loadSnapshots}
          disabled={loading}
        >
          Refresh
        </button>
      </div>

      {error && (
        <div className="mb-2 rounded bg-red-50 px-2 py-1 text-xs text-red-700">
          {error}
        </div>
      )}
      {loading && <div className="text-xs text-gray-400">{"\u6b63\u5728\u52a0\u8f7d\u7248\u672c..."}</div>}
      {!loading && snapshots.length === 0 && (
        <div className="text-xs text-gray-400">{"\u6682\u65e0\u5df2\u4fdd\u5b58\u7248\u672c\u3002"}</div>
      )}

      <div className="space-y-2">
        {snapshots.map((snapshot) => {
          const isActive = snapshot.id === activeSnapshotId;
          return (
            <div
              key={snapshot.id}
              className={`rounded-lg border p-3 ${
                isActive ? "border-indigo-200 bg-indigo-50" : "border-gray-100 bg-gray-50"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-xs font-semibold text-gray-800">
                    v{snapshot.version} - {snapshot.source}
                  </div>
                  <div className="truncate text-[11px] text-gray-500">
                    {snapshot.prompt || "\u672a\u8bb0\u5f55\u63d0\u793a\u8bcd"}
                  </div>
                </div>
                <span
                  className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
                    STATUS_CLASS[snapshot.status] || STATUS_CLASS.unknown
                  }`}
                >
                  {snapshot.status}
                </span>
              </div>
              <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-gray-500">
                <span>{formatTime(snapshot.created_at)}</span>
                <button
                  className="rounded bg-white px-2 py-1 text-indigo-600 shadow-sm hover:text-indigo-700 disabled:text-gray-400"
                  disabled={restoringId === snapshot.id || isActive}
                  onClick={() => restoreSnapshot(snapshot.id)}
                >
                  {isActive
                    ? "\u5f53\u524d\u7248\u672c"
                    : restoringId === snapshot.id
                      ? "\u6062\u590d\u4e2d..."
                      : "\u6062\u590d"}
                </button>
              </div>
              {snapshot.available_exports && snapshot.available_exports.length > 0 && (
                <div className="mt-2 text-[11px] text-blue-700">
                  {"\u5bfc\u51fa\u6587\u4ef6\uff1a"}{snapshot.available_exports.join(", ")}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
