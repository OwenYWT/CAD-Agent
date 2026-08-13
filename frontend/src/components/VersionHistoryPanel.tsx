import { useCallback, useEffect, useMemo, useState } from "react";
import { authFetch } from "../auth";
import type { AssemblyPartInfo, ModelSnapshotDetail, ModelSnapshotSummary } from "../types";

const API_BASE = import.meta.env.VITE_API_BASE || "";

type SnapshotChangeList = {
  added: string[];
  removed: string[];
  changed: string[];
  unchanged: string[];
};

type SnapshotDiff = {
  from_snapshot_id: string;
  to_snapshot_id: string;
  model_changes?: {
    code_changed?: boolean;
    prompt_changed?: boolean;
    source_changed?: boolean;
    inspect_verdict?: { from?: string | null; to?: string | null };
    bounding_box?: { from?: unknown; to?: unknown };
    volume?: { from?: unknown; to?: unknown };
  };
  file_changes?: SnapshotChangeList;
  parameter_changes?: SnapshotChangeList;
  part_changes?: SnapshotChangeList;
};

interface VersionHistoryPanelProps {
  panelId: string;
  activeSnapshotId?: string | null;
  refreshKey?: string | number | null;
  onRestore: (snapshot: ModelSnapshotDetail) => void;
  currentParts?: AssemblyPartInfo[] | null;
  onModifyPart?: (partName: string, instruction: string) => void;
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

function formatVector(position?: number[] | null) {
  if (!position || position.length === 0) return "—";
  return position.map((value) => Number.isFinite(value) ? value.toFixed(2) : String(value)).join(", ");
}

export default function VersionHistoryPanel({
  panelId,
  activeSnapshotId,
  refreshKey,
  onRestore,
  currentParts = [] as AssemblyPartInfo[],
  onModifyPart,
}: VersionHistoryPanelProps) {
  const [snapshots, setSnapshots] = useState<ModelSnapshotSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [diffLoadingKey, setDiffLoadingKey] = useState<string | null>(null);
  const [diffBySnapshotKey, setDiffBySnapshotKey] = useState<Record<string, SnapshotDiff>>({});
  const [selectedPartName, setSelectedPartName] = useState<string>("");
  const [modifyInstruction, setModifyInstruction] = useState("只修改该零件，其他零件代码保持完全不变。");
  const [error, setError] = useState<string | null>(null);

  const parts = currentParts ?? [];
  const selectedPart = useMemo(
    () => parts.find((part) => part.name === selectedPartName) || parts[0] || null,
    [parts, selectedPartName],
  );
  useEffect(() => {
    if (!parts.length) {
      setSelectedPartName("");
      return;
    }
    setSelectedPartName((previous) =>
      parts.some((part) => part.name === previous) ? previous : parts[0].name,
    );
  }, [parts]);

  const loadSnapshots = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await authFetch(`${API_BASE}/api/history/panels/${panelId}/snapshots`);
      if (!response.ok) throw new Error("版本列表加载失败");
      setSnapshots(await response.json());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "版本列表加载失败");
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
      const response = await authFetch(`${API_BASE}/api/history/snapshots/${snapshotId}/restore`, {
        method: "POST",
      });
      if (!response.ok) throw new Error("版本恢复失败");
      onRestore(await response.json());
      await loadSnapshots();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "版本恢复失败");
    } finally {
      setRestoringId(null);
    }
  };

  const loadDiff = async (snapshotId: string) => {
    if (!activeSnapshotId || activeSnapshotId === snapshotId) return;
    const cacheKey = `${activeSnapshotId}:${snapshotId}`;
    setDiffLoadingKey(cacheKey);
    setError(null);
    try {
      const response = await authFetch(`${API_BASE}/api/history/snapshots/${activeSnapshotId}/diff/${snapshotId}`);
      if (!response.ok) throw new Error("对比加载失败");
      const diff = (await response.json()) as SnapshotDiff;
      setDiffBySnapshotKey((previous) => ({ ...previous, [cacheKey]: diff }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "对比加载失败");
    } finally {
      setDiffLoadingKey(null);
    }
  };

  const renderChanges = (label: string, changes?: SnapshotChangeList) => {
    if (!changes) return null;
    return (
      <div className="space-y-0.5">
        <div>
          {label}：+{changes.added.length} / -{changes.removed.length} / ~{changes.changed.length}
        </div>
        {(changes.added.length > 0 || changes.removed.length > 0 || changes.changed.length > 0) ? (
          <div className="text-[10px] text-slate-500">
            {changes.added.length ? `新增 ${changes.added.join(", ")}` : null}
            {changes.removed.length ? `${changes.added.length ? "；" : ""}删除 ${changes.removed.join(", ")}` : null}
            {changes.changed.length ? `${changes.added.length || changes.removed.length ? "；" : ""}变更 ${changes.changed.join(", ")}` : null}
          </div>
        ) : null}
      </div>
    );
  };

  const handleModifyPart = () => {
    if (!onModifyPart) return;
    if (!selectedPart) {
      setError("请先选择一个零件");
      return;
    }
    const instruction = modifyInstruction.trim();
    if (!instruction) {
      setError("请输入零件修改说明");
      return;
    }
    onModifyPart(selectedPart.name, instruction);
  };

  return (
    <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">零件与历史版本</h3>
          <p className="text-xs text-gray-500">直接查看当前拆分零件、触发单零件修改，并对比历史版本。</p>
        </div>
        <button
          className="rounded bg-white px-2 py-1 text-xs text-indigo-600 shadow-sm hover:text-indigo-700 disabled:text-gray-400"
          onClick={loadSnapshots}
          disabled={loading}
          type="button"
        >
          刷新版本
        </button>
      </div>

      {error ? <div className="mb-3 rounded bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div> : null}

      <div className="space-y-3">
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h4 className="text-xs font-semibold text-slate-800">当前零件</h4>
            <span className="text-[11px] text-slate-500">{parts.length} 个</span>
          </div>
          {parts.length > 0 ? (
            <div className="space-y-2">
              <div className="max-h-56 space-y-2 overflow-y-auto pr-1">
                {parts.map((part) => {
                  const isSelected = part.name === selectedPart?.name;
                  return (
                    <button
                      key={part.part_id || part.name}
                      className={`w-full rounded-md border px-3 py-2 text-left transition ${isSelected ? "border-indigo-300 bg-indigo-50" : "border-slate-200 bg-white hover:border-slate-300"}`}
                      onClick={() => setSelectedPartName(part.name)}
                      type="button"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-xs font-medium text-slate-800">{part.name}</div>
                          <div className="mt-0.5 text-[11px] text-slate-500">ID：{part.part_id || "未分配"}</div>
                        </div>
                        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${part.status === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-red-200 bg-red-50 text-red-700"}`}>
                          {part.status === "success" ? "成功" : "失败"}
                        </span>
                      </div>
                      <div className="mt-2 grid gap-1 text-[11px] text-slate-500">
                        <div>Hash：{part.code_hash || "—"}</div>
                        <div>位置：{formatVector(part.position)}</div>
                      </div>
                    </button>
                  );
                })}
              </div>

              <div className="rounded-md border border-slate-200 bg-white p-3">
                <div className="flex items-center justify-between gap-2 text-xs font-medium text-slate-800">
                  <span>零件级修改</span>
                  <span className="text-[11px] text-slate-500">当前选中：{selectedPart?.name || "未选择"}</span>
                </div>
                <textarea
                  className="mt-2 min-h-20 w-full rounded-md border border-slate-200 px-3 py-2 text-xs outline-none focus:border-indigo-400"
                  disabled={!onModifyPart || !selectedPart}
                  onChange={(event) => setModifyInstruction(event.target.value)}
                  placeholder="例如：把边长改为 2cm，并与大正方体接触，其他零件完全不变。"
                  value={modifyInstruction}
                />
                <div className="mt-2 flex items-center justify-between gap-2">
                  <p className="text-[11px] leading-5 text-slate-500">会直接触发零件级修改流程，并尽量保持其他零件代码不变。</p>
                  <button
                    className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-700 disabled:bg-indigo-300"
                    disabled={!onModifyPart || !selectedPart || !modifyInstruction.trim()}
                    onClick={handleModifyPart}
                    type="button"
                  >
                    修改选中零件
                  </button>
                </div>
              </div>
            </div>
          ) : (
            <div className="rounded-md border border-dashed border-slate-300 bg-white p-3 text-xs text-slate-500">
              当前结果还没有拆分出 `assembly_parts`。先生成一个装配体，或者使用零件级修改功能后，这里会显示每个子零件。
            </div>
          )}
        </div>

        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h4 className="text-xs font-semibold text-slate-800">历史版本</h4>
            <span className="text-[11px] text-slate-500">{loading ? "加载中" : `${snapshots.length} 条`}</span>
          </div>

          {!loading && snapshots.length === 0 ? (
            <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 p-3 text-xs text-slate-500">
              暂无已保存版本。
            </div>
          ) : null}

          <div className="space-y-2">
            {snapshots.map((snapshot) => {
              const isActive = snapshot.id === activeSnapshotId;
              const cacheKey = activeSnapshotId ? `${activeSnapshotId}:${snapshot.id}` : "";
              const diff = cacheKey ? diffBySnapshotKey[cacheKey] : undefined;
              return (
                <div
                  key={snapshot.id}
                  className={`rounded-lg border p-3 ${isActive ? "border-indigo-200 bg-indigo-50" : "border-slate-100 bg-slate-50"}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="text-xs font-semibold text-slate-800">
                        v{snapshot.version} · {snapshot.source}
                      </div>
                      <div className="truncate text-[11px] text-slate-500">
                        {snapshot.prompt || "未记录提示词"}
                      </div>
                    </div>
                    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${STATUS_CLASS[snapshot.status] || STATUS_CLASS.unknown}`}>
                      {snapshot.status}
                    </span>
                  </div>

                  <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-slate-500">
                    <span>{formatTime(snapshot.created_at)}</span>
                    <div className="flex items-center gap-2">
                      {activeSnapshotId && !isActive ? (
                        <button
                          className="rounded bg-white px-2 py-1 text-slate-600 shadow-sm hover:text-slate-800 disabled:text-gray-400"
                          disabled={diffLoadingKey === cacheKey}
                          onClick={() => void loadDiff(snapshot.id)}
                          type="button"
                        >
                          {diffLoadingKey === cacheKey ? "对比中..." : "对比当前"}
                        </button>
                      ) : null}
                      <button
                        className="rounded bg-white px-2 py-1 text-indigo-600 shadow-sm hover:text-indigo-700 disabled:text-gray-400"
                        disabled={restoringId === snapshot.id || isActive}
                        onClick={() => void restoreSnapshot(snapshot.id)}
                        type="button"
                      >
                        {isActive ? "当前版本" : restoringId === snapshot.id ? "恢复中..." : "恢复"}
                      </button>
                    </div>
                  </div>

                  {diff ? (
                    <div className="mt-2 rounded-md border border-slate-200 bg-white p-2 text-[11px] text-slate-600">
                      <div className="font-medium text-slate-700">对比结果：当前版本 → v{snapshot.version}</div>
                      <div className="mt-1">模型代码：{diff.model_changes?.code_changed ? "已变化" : "无变化"}</div>
                      <div className="mt-1 space-y-1">
                        {renderChanges("文件", diff.file_changes)}
                        {renderChanges("零件", diff.part_changes)}
                        {renderChanges("参数", diff.parameter_changes)}
                      </div>
                      <div className="mt-1">
                        检查结果：{diff.model_changes?.inspect_verdict?.from || "unknown"} → {diff.model_changes?.inspect_verdict?.to || "unknown"}
                      </div>
                    </div>
                  ) : null}

                  {snapshot.available_exports && snapshot.available_exports.length > 0 ? (
                    <div className="mt-2 text-[11px] text-blue-700">
                      导出文件：{snapshot.available_exports.join(", ")}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}
