import { useCallback, useEffect, useState } from "react";
import { buildChangeSet } from "../../adapters/changeSetAdapter";
import {
  getModelSnapshot,
  listModelSnapshots,
  restoreModelSnapshot,
} from "../../services/engineeringService";
import type { ModelSnapshotDetail } from "../../types";
import type { ChangeSet } from "../../types/engineering";
import { InlineState, WorkspaceDialog } from "../common/WorkspaceOverlay";


interface ChangeSetDialogProps {
  open: boolean;
  panelId: string;
  activeSnapshotId?: string | null;
  onClose: () => void;
  onRestore: (snapshot: ModelSnapshotDetail) => void;
  onAskAgent: (prompt: string) => void;
}

function statusLabel(status: ChangeSet["geometry"]["status"]) {
  if (status === "changed") return "有证据表明已变化";
  if (status === "unchanged") return "现有指标未变化";
  return "未知";
}

function riskLabel(level: ChangeSet["risk"]["level"]) {
  return {
    low: "低",
    medium: "中",
    high: "高",
    unknown: "未知",
  }[level];
}

function fileKindLabel(kind: ChangeSet["files"][number]["kind"]) {
  return { added: "新增", removed: "移除", replaced: "替换" }[kind];
}

function validationLabel(status: ChangeSet["validation"]["status"]) {
  return {
    pass: "通过",
    warning: "有警告",
    fail: "未通过",
    unknown: "未知",
  }[status];
}

export default function ChangeSetDialog({
  open,
  panelId,
  activeSnapshotId,
  onClose,
  onRestore,
  onAskAgent,
}: ChangeSetDialogProps) {
  const [changeSet, setChangeSet] = useState<ChangeSet | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [error, setError] = useState("");
  const [restoring, setRestoring] = useState(false);

  const load = useCallback(async () => {
    if (!open) return;
    setStatus("loading");
    setError("");
    try {
      const snapshots = await listModelSnapshots(panelId);
      const target = activeSnapshotId
        ? snapshots.find((snapshot) => snapshot.id === activeSnapshotId)
        : snapshots[0];
      if (!target) {
        setChangeSet(null);
        setStatus("success");
        return;
      }
      const current = await getModelSnapshot(target.id);
      const base = current.parent_snapshot_id
        ? await getModelSnapshot(current.parent_snapshot_id)
        : null;
      setChangeSet(buildChangeSet(current, base));
      setStatus("success");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "变更证据加载失败");
      setStatus("error");
    }
  }, [activeSnapshotId, open, panelId]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, open]);

  const rollback = async () => {
    if (!changeSet?.baseRevisionId) return;
    setRestoring(true);
    setError("");
    try {
      const restored = await restoreModelSnapshot(changeSet.baseRevisionId);
      onRestore(restored);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "回滚失败");
    } finally {
      setRestoring(false);
    }
  };

  const requestModification = () => {
    if (!changeSet) return;
    const parameterSummary = changeSet.parameterChanges.length
      ? changeSet.parameterChanges
        .map((change) => `${change.label}：${change.before} → ${change.after}${change.unit || ""}`)
        .join("；")
      : "没有可证实的参数差异";
    onAskAgent(
      `请基于当前变更继续修改。目标：${changeSet.objective || "未记录"}。`
      + `已证实参数变化：${parameterSummary}。`
      + `风险：${changeSet.risk.reasons.join("；")}。`,
    );
  };

  return (
    <WorkspaceDialog
      description="仅展示版本快照、任务结果、参数、几何指标、文件引用和验证记录能够证实的变化；缺失证据保持未知。"
      footer={(
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="mr-auto text-[11px] text-[var(--faint)]">
            接受变更和提交版本尚无后端接口，当前不可用。
          </span>
          <button className="workspace-button" onClick={onClose} type="button">关闭</button>
          <button
            className="workspace-button"
            disabled={!changeSet}
            onClick={requestModification}
            type="button"
          >
            请求修改
          </button>
          <button
            className="workspace-button"
            disabled={!changeSet?.baseRevisionId || restoring}
            onClick={() => void rollback()}
            type="button"
          >
            {restoring ? "正在回滚" : "拒绝并回滚"}
          </button>
          <button className="workspace-button" disabled title="后端尚无接受 Change Set 接口" type="button">
            接受变更
          </button>
          <button className="workspace-button workspace-button--primary" disabled title="后端尚无提交版本接口" type="button">
            提交版本
          </button>
        </div>
      )}
      onClose={onClose}
      open={open}
      title="变更审查"
    >
      <div className="space-y-5 p-5">
        {status === "loading" ? (
          <InlineState detail="正在读取当前版本及其明确记录的父版本。" title="正在加载变更证据" tone="info" />
        ) : null}
        {status === "error" ? (
          <InlineState actionLabel="重试" detail={error} onAction={() => void load()} title="变更证据加载失败" tone="error" />
        ) : null}
        {status === "success" && !changeSet ? (
          <InlineState detail="当前面板还没有真实模型快照。完成一次 CAD 生成或参数执行后再查看变更。" title="暂无可审查版本" />
        ) : null}
        {error && status !== "error" ? (
          <InlineState detail={error} title="操作失败" tone="error" />
        ) : null}

        {changeSet ? (
          <>
            <section className="border-b border-[var(--line)] pb-4">
              <div className="grid gap-3 text-xs sm:grid-cols-3">
                <div><p className="text-[var(--faint)]">修改目标</p><p className="mt-1 text-[var(--ink)]">{changeSet.objective || "未记录"}</p></div>
                <div><p className="text-[var(--faint)]">版本关系</p><p className="mt-1 text-[var(--ink)]">{changeSet.baseVersion ? `v${changeSet.baseVersion} → v${changeSet.targetVersion}` : `父版本未知 → v${changeSet.targetVersion}`}</p></div>
                <div><p className="text-[var(--faint)]">修改对象数</p><p className="mt-1 text-[var(--ink)]">{changeSet.modifiedObjectCount ?? "未知"}</p></div>
              </div>
              <p className="mt-3 break-all text-[10px] text-[var(--faint)]">
                任务 {changeSet.taskId || "未记录"} · 请求 {changeSet.requestId || "未记录"}
              </p>
            </section>

            <section>
              <h3 className="text-xs font-semibold text-[var(--ink)]">参数变化</h3>
              {changeSet.parameterChanges.length ? (
                <div className="mt-2 overflow-hidden rounded-lg border border-[var(--line)]">
                  {changeSet.parameterChanges.map((change) => (
                    <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-[var(--line)] px-3 py-2 text-xs last:border-b-0" key={change.parameterId}>
                      <span className="truncate text-[var(--ink)]">{change.label}</span>
                      <span className="text-[var(--muted)]">{change.before} → {change.after}{change.unit || ""}</span>
                    </div>
                  ))}
                </div>
              ) : <p className="mt-2 text-xs text-[var(--faint)]">没有父版本参数证据，或已记录参数值未变化。</p>}
            </section>

            <section className="grid gap-4 sm:grid-cols-2">
              <div>
                <h3 className="text-xs font-semibold text-[var(--ink)]">代码与几何</h3>
                <dl className="mt-2 space-y-2 text-xs text-[var(--muted)]">
                  <div className="flex justify-between gap-3"><dt>代码</dt><dd>{statusLabel(changeSet.code.status)}</dd></div>
                  <div className="flex justify-between gap-3"><dt>几何</dt><dd>{statusLabel(changeSet.geometry.status)}</dd></div>
                  {changeSet.geometry.metrics.map((metric) => (
                    <div className="flex justify-between gap-3" key={metric.label}><dt>{metric.label}</dt><dd>{metric.before} → {metric.after} {metric.unit}</dd></div>
                  ))}
                </dl>
              </div>
              <div>
                <h3 className="text-xs font-semibold text-[var(--ink)]">验证与风险</h3>
                <p className="mt-2 text-xs text-[var(--muted)]">验证：{validationLabel(changeSet.validation.status)} · {changeSet.validation.summary}</p>
                <p className="mt-2 text-xs text-[var(--muted)]">风险：{riskLabel(changeSet.risk.level)}</p>
                <ul className="mt-1 space-y-1 text-[11px] text-[var(--faint)]">
                  {changeSet.risk.reasons.map((reason) => <li key={reason}>· {reason}</li>)}
                </ul>
              </div>
            </section>

            <section>
              <h3 className="text-xs font-semibold text-[var(--ink)]">文件变化</h3>
              {changeSet.files.length ? (
                <div className="mt-2 space-y-2">
                  {changeSet.files.map((file) => (
                    <div className="rounded-lg border border-[var(--line)] px-3 py-2 text-xs" key={`${file.format}:${file.kind}`}>
                      <p className="text-[var(--ink)]">{file.format} · {fileKindLabel(file.kind)}</p>
                      <p className="mt-1 break-all text-[10px] text-[var(--faint)]">仅能证实文件引用变化；当前元数据没有 SHA-256，不能声称内容已变化。</p>
                    </div>
                  ))}
                </div>
              ) : <p className="mt-2 text-xs text-[var(--faint)]">没有可证实的文件引用变化。</p>}
            </section>

            <section>
              <h3 className="text-xs font-semibold text-[var(--ink)]">Agent 操作日志</h3>
              {changeSet.agentLogs.length ? (
                <ol className="mt-2 space-y-1 text-xs text-[var(--muted)]">
                  {changeSet.agentLogs.map((log) => <li key={log}>{log}</li>)}
                </ol>
              ) : <p className="mt-2 text-xs text-[var(--faint)]">当前快照没有 repair_history，未显示推测日志。</p>}
            </section>
          </>
        ) : null}
      </div>
    </WorkspaceDialog>
  );
}
