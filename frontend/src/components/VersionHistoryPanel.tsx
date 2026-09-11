import { useCallback, useEffect, useMemo, useState } from "react";
import { useI18n, type Locale } from "../i18n/I18nContext";
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
  onRestore: (snapshot: ModelSnapshotDetail) => boolean | void;
  currentParts?: AssemblyPartInfo[] | null;
  onModifyPart?: (partName: string, instruction: string) => boolean | void;
}

const STATUS_CLASS: Record<string, string> = {
  pass: "border-emerald-200 bg-emerald-50 text-emerald-700",
  warn: "border-amber-200 bg-amber-50 text-amber-700",
  fail: "border-red-200 bg-red-50 text-red-700",
  unknown: "border-[var(--line)] bg-[var(--subtle)] text-[var(--muted)]",
};

function formatTime(iso: string, locale: Locale) {
  try {
    return new Date(iso).toLocaleString(locale === "zh" ? "zh-CN" : "en-US");
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
  const { locale, translate } = useI18n();
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
      const accepted = onRestore(await getModelSnapshot(snapshotId));
      if (accepted === false) throw new Error("当前连接不可用，未提交版本恢复任务");
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
      const diff = await diffModelSnapshots(activeSnapshotId, snapshotId);
      setDiffBySnapshotKey((previous) => ({ ...previous, [cacheKey]: diff }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "版本对比加载失败");
    } finally {
      setDiffLoadingKey(null);
    }
  };

  const renderChanges = (label: string, changes?: SnapshotChangeList) => {
    if (!changes) return <div>{label}：缺少可比较的证据</div>;
    return (
      <div className="space-y-0.5">
        <div>{label}：+{changes.added.length} / -{changes.removed.length} / ~{changes.changed.length}</div>
        {changes.added.length || changes.removed.length || changes.changed.length ? (
          <div className="type-caption text-[var(--muted)]">
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
    <section className="bg-[var(--surface)] p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="type-section-heading  text-[var(--ink)]">零件与历史版本</h3>
          <p className="type-body text-[var(--muted)]">原生版本从 FCStd 恢复，源码版本重新执行已保存代码；生成候选后需审查提交，才会成为当前版本。</p>
        </div>
        <button className="workspace-button" disabled={loading} onClick={loadSnapshots} type="button">刷新版本</button>
      </div>

      {error ? <div className="mb-3 rounded bg-red-50 px-3 py-2 type-body text-red-700">{error}</div> : null}

      <div className="space-y-3">
        <div className="rounded-lg border border-[var(--line)] bg-[var(--surface-soft)] p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h4 className="type-section-heading  text-[var(--ink)]">当前零件</h4>
            <span className="type-caption text-[var(--muted)]">{translate(`${parts.length} 个`)}</span>
          </div>
          {parts.length ? (
            <div className="space-y-2">
              <div className="max-h-56 space-y-2 overflow-y-auto pr-1">
                {parts.map((part) => {
                  const isSelected = part.name === selectedPart?.name;
                  return (
                    <button key={part.part_id || part.name} className={`w-full rounded-md border px-3 py-2 text-left transition ${isSelected ? "border-[var(--agent-border)] bg-[var(--agent-soft)]" : "border-[var(--line)] bg-[var(--surface)] hover:border-[var(--line-strong)]"}`} onClick={() => setSelectedPartName(part.name)} type="button">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate type-body  text-[var(--ink)]">{part.name}</div>
                          <div className="mt-0.5 type-caption text-[var(--muted)]">ID：{part.part_id || "未分配"}</div>
                        </div>
                        <span className={`shrink-0 rounded-full border px-2 py-0.5 type-caption  ${part.status === "success" ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-red-200 bg-red-50 text-red-700"}`}>{part.status === "success" ? "成功" : "失败"}</span>
                      </div>
                      <div className="mt-2 grid gap-1 type-caption text-[var(--muted)]">
                        <div>Hash：{part.code_hash || "—"}</div>
                        <div>位置：{formatVector(part.position)}</div>
                      </div>
                    </button>
                  );
                })}
              </div>
              <div className="rounded-md border border-[var(--line)] bg-[var(--surface)] p-3">
                <div className="flex items-center justify-between gap-2 type-body  text-[var(--ink)]">
                  <span>零件级修改</span>
                  <span className="type-caption text-[var(--muted)]">当前：{selectedPart?.name || "未选择"}</span>
                </div>
                <textarea className="mt-2 min-h-20 w-full rounded-md border border-[var(--line)] px-3 py-2 type-control outline-none focus:border-[var(--focus)]" disabled={!onModifyPart || !selectedPart} onChange={(event) => setModifyInstruction(event.target.value)} placeholder="例如：把边长改为 2cm，并与大正方体接触。" value={modifyInstruction} />
                <div className="mt-2 flex items-center justify-between gap-2">
                  <p className="type-caption  text-[var(--muted)]">修改会进入持久任务与 Change Set 审查流程。</p>
                  <button className="workspace-button workspace-button--primary" disabled={!onModifyPart || !selectedPart || !modifyInstruction.trim()} onClick={handleModifyPart} type="button">修改选中零件</button>
                </div>
              </div>
            </div>
          ) : (
            <div className="rounded-md border border-dashed border-[var(--line-strong)] bg-[var(--surface)] p-3 type-body text-[var(--muted)]">当前结果尚未包含装配零件。生成装配体后会在这里显示子零件。</div>
          )}
        </div>

        <div className="rounded-lg border border-[var(--line)] bg-[var(--surface)] p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h4 className="type-section-heading  text-[var(--ink)]">历史版本</h4>
            <span className="type-caption text-[var(--muted)]">{translate(loading ? "加载中" : `${snapshots.length} 条`)}</span>
          </div>
          {!loading && !snapshots.length ? <div className="rounded-md border border-dashed border-[var(--line-strong)] bg-[var(--surface-soft)] p-3 type-body text-[var(--muted)]">暂无已保存版本。</div> : null}
          <div className="space-y-2">
            {snapshots.map((snapshot) => {
              const isActive = snapshot.id === activeSnapshotId;
              const cacheKey = activeSnapshotId ? `${activeSnapshotId}:${snapshot.id}` : "";
              const diff = cacheKey ? diffBySnapshotKey[cacheKey] : undefined;
              return (
                <div key={snapshot.id} className={`rounded-lg border p-3 ${isActive ? "border-[var(--agent-border)] bg-[var(--agent-soft)]" : "border-[var(--line)] bg-[var(--surface-soft)]"}`}>
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="type-body  text-[var(--ink)]">v{snapshot.version} · {translate(engineeringSourceLabel(snapshot.source))}</div>
                      <div className="truncate type-caption text-[var(--muted)]">{engineeringPromptLabel(snapshot.prompt)}</div>
                    </div>
                    <span className={`rounded-full border px-2 py-0.5 type-caption  ${STATUS_CLASS[snapshot.status] || STATUS_CLASS.unknown}`}>{translate(engineeringStatusLabel(snapshot.status))}</span>
                  </div>
                  <div className="mt-2 flex items-center justify-between gap-2 type-caption text-[var(--muted)]">
                    <span>{formatTime(snapshot.created_at, locale)}</span>
                    <div className="flex items-center gap-2">
                      {activeSnapshotId && !isActive ? <button className="workspace-button !min-h-7 !px-2" disabled={diffLoadingKey === cacheKey} onClick={() => void loadDiff(snapshot.id)} type="button">{diffLoadingKey === cacheKey ? "对比中..." : "对比当前"}</button> : null}
                      <button className="workspace-button !min-h-7 !px-2 text-[var(--focus)]" disabled={restoringId !== null || isActive} onClick={() => void restoreSnapshot(snapshot.id)} type="button">{isActive ? "当前版本" : restoringId === snapshot.id ? "提交中..." : "恢复"}</button>
                    </div>
                  </div>
                  {diff ? (
                    <div className="mt-2 rounded-md border border-[var(--line)] bg-[var(--surface)] p-2 type-caption text-[var(--muted)]">
                      <div className=" text-[var(--ink)]">对比结果：当前版本 → v{snapshot.version}</div>
                      <div className="mt-1">模型代码：{typeof diff.model_changes?.code_changed === "boolean" ? diff.model_changes.code_changed ? "已变化" : "无变化" : "无可比较的源码（原生模型以文件与参数为准）"}</div>
                      <div className="mt-1 space-y-1">{renderChanges("文件", diff.file_changes)}{renderChanges("零件", diff.part_changes)}{renderChanges("参数", diff.parameter_changes)}</div>
                      <div className="mt-1">检查结果：{diff.model_changes?.inspect_verdict?.from || "unknown"} → {diff.model_changes?.inspect_verdict?.to || "unknown"}</div>
                    </div>
                  ) : null}
                  {snapshot.available_exports?.length ? <div className="mt-2 type-caption text-[var(--focus)]">{translate("导出文件")}：{snapshot.available_exports.join(", ")}</div> : null}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}
