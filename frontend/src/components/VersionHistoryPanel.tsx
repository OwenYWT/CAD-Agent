import { useCallback, useEffect, useMemo, useState } from "react";
import {
  diffModelSnapshots,
  getModelSnapshot,
  listModelSnapshots,
} from "../services/engineeringService";
import type {
  AssemblyPartInfo,
  ModelSnapshotDetail,
  ModelSnapshotDiff,
  ModelSnapshotSummary,
  SnapshotChangeList,
} from "../types";
import {
  engineeringPromptLabel,
  engineeringSourceLabel,
  engineeringStatusLabel,
} from "../utils/engineeringLabels";
interface VersionHistoryPanelProps {
  panelId: string;
  activeSnapshotId?: string | null;
  refreshKey?: string | number | null;
  onRestore: (snapshot: ModelSnapshotDetail) => boolean | void | Promise<boolean | void>;
  currentParts?: AssemblyPartInfo[] | null;
  onModifyPart?: (partName: string, instruction: string) => boolean | void;
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
  if (!position?.length) return "—";
  return position.map((value) => Number.isFinite(value) ? value.toFixed(2) : String(value)).join(", ");
}
export default function VersionHistoryPanel({
  panelId,
  activeSnapshotId,
  refreshKey,
  onRestore,
  currentParts = [],
  onModifyPart,
}: VersionHistoryPanelProps) {
  const [snapshots, setSnapshots] = useState<ModelSnapshotSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [diffLoadingKey, setDiffLoadingKey] = useState<string | null>(null);
  const [diffBySnapshotKey, setDiffBySnapshotKey] = useState<Record<string, ModelSnapshotDiff>>({});
  const [selectedPartName, setSelectedPartName] = useState("");
  const [modifyInstruction, setModifyInstruction] = useState("只修改该零件，其他零件代码保持完全不变。");
  const [error, setError] = useState<string | null>(null);
  const parts = useMemo(() => currentParts ?? [], [currentParts]);
  const selectedPart = useMemo(
    () => parts.find((part) => part.name === selectedPartName) || parts[0] || null,
    [parts, selectedPartName],
  );
  const loadSnapshots = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSnapshots(await listModelSnapshots(panelId));
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
      const accepted = await onRestore(await getModelSnapshot(snapshotId));
      if (accepted === false) throw new Error("Restore request was rejected.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Snapshot restore failed");
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
      const diff = await diffModelSnapshots(activeSnapshotId, snapshotId);
      setDiffBySnapshotKey((previous) => ({ ...previous, [cacheKey]: diff }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "版本对比加载失败");
    } finally {
      setDiffLoadingKey(null);
    }
  };
  const renderChanges = (label: string, changes?: SnapshotChangeList) => {
    if (!changes) return null;
    return (
      <div className="space-y-0.5">
        <div>{label}：+{changes.added.length} / -{changes.removed.length} / ~{changes.changed.length}</div>
        {changes.added.length || changes.removed.length || changes.changed.length ? (
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
    if (!onModifyPart || !selectedPart) {
      setError("请先选择一个零件");
      return;
    }
    const instruction = modifyInstruction.trim();
    if (!instruction) {
      setError("请输入零件修改说明");
      return;
    }
    setError(null);
    const accepted = onModifyPart(selectedPart.name, instruction);
    if (accepted === false) {
      setError("当前连接不可用，未提交零件修改任务");
    }
  };
  return (
    <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">零件与历史版本</h3>
          <p className="text-xs text-gray-500">恢复会重新执行已保存代码，并生成可审查的 Change Set。</p>
        </div>
        <button className="rounded bg-white px-2 py-1 text-xs text-indigo-600 shadow-sm hover:text-indigo-700 disabled:text-gray-400" disabled={loading} onClick={loadSnapshots} type="button">刷新版本</button>
      </div>
      {error ? <div className="mb-3 rounded bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div> : null}
      <div className="space-y-3">
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h4 className="text-xs font-semibold text-slate-800">当前零件</h4>
            <span className="text-[11px] text-slate-500">{parts.length} 个</span>
          </div>
          {parts.length ? (
            <div className="space-y-2">
              <div className="max-h-56 space-y-2 overflow-y-auto pr-1">
                {parts.map((part) => {
                  const isSelected = part.name === selectedPart?.name;
                  return (
                    <button key={part.part_id || part.name} className={`w-full rounded-md border px-3 py-2 text-left transition ${isSelected ? "border-indigo-300 bg-indigo-50" : "border-slate-200 bg-white hover:border-slate-300"}`} onClick={() => setSelectedPartName(part.name)} type="button">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-xs font-medium text-slate-800">{part.name}</div>
                          <div className="mt-0.5 text-[11px] text-slate-500">ID：{part.part_id || "未分配"}</div>
                        </div>
                        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${part.status === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-red-200 bg-red-50 text-red-700"}`}>{part.status === "success" ? "成功" : "失败"}</span>
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
                  <span className="text-[11px] text-slate-500">当前：{selectedPart?.name || "未选择"}</span>
                </div>
                <textarea className="mt-2 min-h-20 w-full rounded-md border border-slate-200 px-3 py-2 text-xs outline-none focus:border-indigo-400" disabled={!onModifyPart || !selectedPart} onChange={(event) => setModifyInstruction(event.target.value)} placeholder="例如：把边长改为 2cm，并与大正方体接触。" value={modifyInstruction} />
                <div className="mt-2 flex items-center justify-between gap-2">
                  <p className="text-[11px] leading-5 text-slate-500">修改会进入持久任务与 Change Set 审查流程。</p>
                  <button className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-700 disabled:bg-indigo-300" disabled={!onModifyPart || !selectedPart || !modifyInstruction.trim()} onClick={handleModifyPart} type="button">修改选中零件</button>
                </div>
              </div>
            </div>
          ) : (
            <div className="rounded-md border border-dashed border-slate-300 bg-white p-3 text-xs text-slate-500">当前结果尚未包含装配零件。生成装配体后会在这里显示子零件。</div>
          )}
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h4 className="text-xs font-semibold text-slate-800">历史版本</h4>
            <span className="text-[11px] text-slate-500">{loading ? "加载中" : `${snapshots.length} 条`}</span>
          </div>
          {!loading && !snapshots.length ? <div className="rounded-md border border-dashed border-slate-300 bg-slate-50 p-3 text-xs text-slate-500">暂无已保存版本。</div> : null}
          <div className="space-y-2">
            {snapshots.map((snapshot) => {
              const isActive = snapshot.id === activeSnapshotId;
              const cacheKey = activeSnapshotId ? `${activeSnapshotId}:${snapshot.id}` : "";
              const diff = cacheKey ? diffBySnapshotKey[cacheKey] : undefined;
              return (
                <div key={snapshot.id} className={`rounded-lg border p-3 ${isActive ? "border-indigo-200 bg-indigo-50" : "border-slate-100 bg-slate-50"}`}>
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="text-xs font-semibold text-slate-800">v{snapshot.version} · {engineeringSourceLabel(snapshot.source)}</div>
                      <div className="truncate text-[11px] text-slate-500">{engineeringPromptLabel(snapshot.prompt)}</div>
                    </div>
                    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${STATUS_CLASS[snapshot.status] || STATUS_CLASS.unknown}`}>{engineeringStatusLabel(snapshot.status)}</span>
                  </div>
                  <div className="mt-2 flex items-center justify-between gap-2 text-[11px] text-slate-500">
                    <span>{formatTime(snapshot.created_at)}</span>
                    <div className="flex items-center gap-2">
                      {activeSnapshotId && !isActive ? <button className="rounded bg-white px-2 py-1 text-slate-600 shadow-sm hover:text-slate-800 disabled:text-gray-400" disabled={diffLoadingKey === cacheKey} onClick={() => void loadDiff(snapshot.id)} type="button">{diffLoadingKey === cacheKey ? "对比中..." : "对比当前"}</button> : null}
                      <button className="rounded bg-white px-2 py-1 text-indigo-600 shadow-sm hover:text-indigo-700 disabled:text-gray-400" disabled={restoringId === snapshot.id || isActive} onClick={() => void restoreSnapshot(snapshot.id)} type="button">{isActive ? "当前版本" : restoringId === snapshot.id ? "提交中..." : "恢复"}</button>
                    </div>
                  </div>
                  {diff ? (
                    <div className="mt-2 rounded-md border border-slate-200 bg-white p-2 text-[11px] text-slate-600">
                      <div className="font-medium text-slate-700">对比结果：当前版本 → v{snapshot.version}</div>
                      <div className="mt-1">模型代码：{diff.model_changes?.code_changed ? "已变化" : "无变化"}</div>
                      <div className="mt-1 space-y-1">{renderChanges("文件", diff.file_changes)}{renderChanges("零件", diff.part_changes)}{renderChanges("参数", diff.parameter_changes)}</div>
                      <div className="mt-1">检查结果：{diff.model_changes?.inspect_verdict?.from || "unknown"} → {diff.model_changes?.inspect_verdict?.to || "unknown"}</div>
                    </div>
                  ) : null}
                  {snapshot.available_exports?.length ? <div className="mt-2 text-[11px] text-blue-700">导出文件：{snapshot.available_exports.join(", ")}</div> : null}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}
