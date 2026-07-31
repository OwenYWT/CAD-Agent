import { useCallback, useEffect, useState } from "react";
import {
  getModelSnapshot,
  listModelSnapshots,
} from "../services/engineeringService";
import type { ModelSnapshotDetail, ModelSnapshotSummary } from "../types";
import {
  engineeringPromptLabel,
  engineeringSourceLabel,
  engineeringStatusLabel,
} from "../utils/engineeringLabels";

interface VersionHistoryPanelProps {
  panelId: string;
  activeSnapshotId?: string | null;
  refreshKey?: string | number | null;
  onRestore: (snapshot: ModelSnapshotDetail) => boolean | void;
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
      setSnapshots(await listModelSnapshots(panelId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "\u7248\u672c\u5217\u8868\u52a0\u8f7d\u5931\u8d25");
    } finally {
      setLoading(false);
    }
  }, [panelId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadSnapshots(), 0);
    return () => window.clearTimeout(timer);
  }, [loadSnapshots, refreshKey]);

  const restoreSnapshot = async (snapshotId: string) => {
    setRestoringId(snapshotId);
    setError(null);
    try {
      const accepted = onRestore(await getModelSnapshot(snapshotId));
      if (accepted === false) {
        throw new Error("当前连接不可用，未提交版本恢复任务");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "版本恢复失败");
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
            恢复会重新执行已保存代码，并生成可审查的 Change Set。
          </p>
        </div>
        <button
          className="text-xs text-indigo-600 hover:text-indigo-700 disabled:text-gray-400"
          onClick={loadSnapshots}
          disabled={loading}
        >
          刷新
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
                    v{snapshot.version} · {engineeringSourceLabel(snapshot.source)}
                  </div>
                  <div className="truncate text-[11px] text-gray-500">
                    {engineeringPromptLabel(snapshot.prompt)}
                  </div>
                </div>
                <span
                  className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
                    STATUS_CLASS[snapshot.status] || STATUS_CLASS.unknown
                  }`}
                >
                  {engineeringStatusLabel(snapshot.status)}
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
