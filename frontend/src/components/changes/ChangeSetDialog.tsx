import { useCallback, useEffect, useRef, useState } from "react";
import {
  adaptDurableChangeSet,
  buildChangeSet,
  durableChangeSetAcceptanceState,
  durableChangeSetCanCommit,
} from "../../adapters/changeSetAdapter";
import type { DurableChangeSetAcceptanceBlockReason } from "../../adapters/changeSetAdapter";
import { acceptDurableChangeSet, commitDurableChangeSet, getDurableChangeSet, rejectDurableChangeSet, requestDurableChangeSetModification, rollbackDurableChangeSet } from "../../services/clients/changes";
import { getModelSnapshot, listModelSnapshots } from "../../services/clients/revisions";
import type { ModelSnapshotDetail } from "../../types";
import type {
  ChangeSet,
  DurableChangeSetDetail,
} from "../../types/engineering";
import { InlineState, WorkspaceDialog } from "../common/WorkspaceOverlay";


interface ChangeSetDialogProps {
  open: boolean;
  panelId: string;
  activeSnapshotId?: string | null;
  changeSetId?: string | null;
  onClose: () => void;
  onRestore?: (snapshot: ModelSnapshotDetail) => boolean | void | Promise<boolean | void>;
  onAskAgent?: (prompt: string, continuation?: { candidateRevisionId: string; baseRevisionId: string }) => void;
  onDurableChangeSet?: (detail: DurableChangeSetDetail) => void;
  canCommit?: boolean;
  onReviewed?: (status: "committed" | "rolled_back" | "rejected" | "changes_requested") => void | Promise<void>;
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

function acceptanceBlockLabel(
  reason: DurableChangeSetAcceptanceBlockReason,
) {
  return {
    not_durable: "当前查看的是历史快照，只能恢复或继续修改，不能直接接受变更。",
    not_pending_review: "当前变更已不在待审阅状态，请刷新后再操作。",
    validation_failed: "存在未通过的必需验证，修复验证问题后才能接受变更。",
    validation_unknown: "缺少可判定的验证证据，验证完成后才能接受变更。",
    review_note_required: "存在审阅风险，请先填写审阅意见后再接受变更。",
  }[reason];
}

export default function ChangeSetDialog({
  open,
  panelId,
  activeSnapshotId,
  changeSetId,
  onClose,
  onRestore,
  onAskAgent,
  onDurableChangeSet,
  canCommit = true,
  onReviewed,
}: ChangeSetDialogProps) {
  const [changeSet, setChangeSet] = useState<ChangeSet | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [error, setError] = useState("");
  const [restoring, setRestoring] = useState(false);
  const [reviewNote, setReviewNote] = useState("");
  const [detail, setDetail] = useState<DurableChangeSetDetail | null>(null);
  const generation = useRef(0);
  const active = useRef(false);
  useEffect(() => {
    active.current = open;
    generation.current += 1;
    return () => { active.current = false; generation.current += 1; };
  }, [open, panelId, changeSetId]);

  const load = useCallback(async () => {
    if (!open || !active.current) return;
    const requestGeneration = generation.current;
    setStatus("loading");
    setError("");
    try {
      if (changeSetId) {
        const detail = await getDurableChangeSet(changeSetId);
        if (generation.current !== requestGeneration) return;
        setDetail(detail);
        onDurableChangeSet?.(detail);
        setChangeSet(adaptDurableChangeSet(detail, panelId));
        setReviewNote(detail.review_note || "");
        setStatus("success");
        return;
      }
      const snapshots = await listModelSnapshots(panelId);
      const target = activeSnapshotId
        ? snapshots.find((snapshot) => snapshot.id === activeSnapshotId)
        : snapshots[0];
      if (!target) {
        setChangeSet(null);
        setReviewNote("");
        setStatus("success");
        return;
      }
      const current = await getModelSnapshot(target.id);
      const base = current.parent_snapshot_id
        ? await getModelSnapshot(current.parent_snapshot_id)
        : null;
      if (generation.current !== requestGeneration) return;
      setDetail(null);
      setChangeSet(buildChangeSet(current, base));
      setReviewNote("");
      setStatus("success");
    } catch (reason) {
      if (generation.current !== requestGeneration) return;
      setError(reason instanceof Error ? reason.message : "变更证据加载失败");
      setStatus("error");
    }
  }, [
    activeSnapshotId,
    changeSetId,
    onDurableChangeSet,
    open,
    panelId,
  ]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, open]);

  useEffect(() => {
    if (
      !open
      || changeSet?.source !== "durable"
      || changeSet.reviewStatus !== "accepted"
      || !changeSet.taskId
      || ["succeeded", "failed", "cancelled", "timed_out"].includes(
        changeSet.workflowStatus || "",
      )
    ) return;
    const timer = window.setInterval(() => void load(), 2000);
    return () => window.clearInterval(timer);
  }, [changeSet?.reviewStatus, changeSet?.source, changeSet?.taskId, changeSet?.workflowStatus, load, open]);

  const acceptanceState = durableChangeSetAcceptanceState(changeSet, reviewNote);
  const acceptanceHint = !changeSet
    ? null
    : acceptanceState.reason
      ? acceptanceBlockLabel(acceptanceState.reason)
      : detail?.can_review !== true
        ? "服务器尚未确认当前用户可以审阅此变更。"
        : detail?.base_is_current !== true
          ? "候选基线已不是当前版本，请刷新后重新生成变更。"
          : null;
  const acceptanceReady = acceptanceState.canAccept
    && detail?.can_review === true
    && detail.base_is_current === true;
  const commitReady = changeSet
    ? durableChangeSetCanCommit(changeSet, canCommit)
      && detail?.can_commit === true
      && detail.base_is_current === true
    : false;
  const workflowStillRunning = Boolean(
    changeSet?.source === "durable"
    && changeSet.reviewStatus === "accepted"
    && changeSet.taskId
    && changeSet.workflowStatus !== "succeeded"
    && !["failed", "cancelled", "timed_out"].includes(changeSet.workflowStatus || ""),
  );

  const rollback = async () => {
    if (!changeSet?.baseRevisionId) return;
    const actionGeneration = generation.current;
    setRestoring(true);
    setError("");
    try {
      if (changeSet.source === "durable") {
        if (changeSet.reviewStatus === "committed") {
          await rollbackDurableChangeSet(changeSet.id, reviewNote);
        } else {
          if (!reviewNote.trim()) {
            throw new Error("拒绝变更前请填写审查意见");
          }
          await rejectDurableChangeSet(changeSet.id, reviewNote.trim());
        }
        if (generation.current !== actionGeneration) return;
        await load();
        if (generation.current === actionGeneration) await onReviewed?.(changeSet.reviewStatus === "committed" ? "rolled_back" : "rejected");
        return;
      }
      const restored = await getModelSnapshot(changeSet.baseRevisionId);
      if (generation.current !== actionGeneration) return;
      if (!onRestore) throw new Error("请从原始历史面板恢复此快照");
      const accepted = await onRestore(restored);
      if (accepted === false) {
        throw new Error("当前连接不可用，未提交回滚任务");
      }
      if (generation.current === actionGeneration) onClose();
    } catch (reason) {
      if (generation.current === actionGeneration) setError(reason instanceof Error ? reason.message : "回滚失败");
    } finally {
      if (generation.current === actionGeneration) setRestoring(false);
    }
  };

  const requestModification = async () => {
    if (!changeSet) return;
    const actionGeneration = generation.current;
    if (changeSet.source === "durable") {
      if (!reviewNote.trim()) {
        setError("请求修改前请填写审查意见");
        return;
      }
      try {
        await requestDurableChangeSetModification(
          changeSet.id,
          reviewNote.trim(),
        );
      } catch (reason) {
        if (generation.current === actionGeneration) setError(reason instanceof Error ? reason.message : "请求修改失败");
        return;
      }
      if (generation.current !== actionGeneration) return;
      await load();
      if (generation.current !== actionGeneration) return;
      await onReviewed?.("changes_requested");
      if (!onAskAgent) return;
    }
    const parameterSummary = changeSet.parameterChanges.length
      ? changeSet.parameterChanges
        .map((change) => `${change.label}：${change.before} → ${change.after}${change.unit || ""}`)
        .join("；")
      : "没有可证实的参数差异";
    onAskAgent?.(
      `请基于当前变更继续修改。目标：${changeSet.objective || "未记录"}。`
      + `审查意见：${reviewNote.trim() || "未填写"}。`
      + `已证实参数变化：${parameterSummary}。`
      + `风险：${changeSet.risk.reasons.join("；")}。`,
      changeSet.source === "durable" && changeSet.baseRevisionId ? { candidateRevisionId: changeSet.targetRevisionId, baseRevisionId: changeSet.baseRevisionId } : undefined,
    );
  };

  const accept = async () => {
    if (!changeSet || changeSet.source !== "durable") return;
    if (!acceptanceReady) {
      setError(acceptanceHint || "当前变更尚未满足接受条件，请刷新后重试。");
      return;
    }
    const actionGeneration = generation.current;
    setRestoring(true);
    setError("");
    try {
      await acceptDurableChangeSet(changeSet.id, reviewNote.trim());
      if (generation.current !== actionGeneration) return;
      await load();
    } catch (reason) {
      if (generation.current === actionGeneration) setError(reason instanceof Error ? reason.message : "接受变更失败");
    } finally {
      if (generation.current === actionGeneration) setRestoring(false);
    }
  };

  const apply = async () => {
    if (!changeSet || changeSet.source !== "durable") return;
    const id = changeSet.id, actionGeneration = generation.current;
    const present = async () => {
      const current = await getDurableChangeSet(id);
      if (generation.current === actionGeneration) {
        setDetail(current); setChangeSet(adaptDurableChangeSet(current, panelId)); onDurableChangeSet?.(current);
      }
      return current;
    };
    setRestoring(true); setError("");
    try {
      let current = await present();
      if (generation.current !== actionGeneration) return;
      if (current.status === "pending_review") {
        try { await acceptDurableChangeSet(id, reviewNote.trim()); }
        catch (reason) {
          current = await present();
          if (!["accepted", "committed"].includes(current.status)) throw reason;
        }
        current = await present();
      }
      if (generation.current !== actionGeneration) return;
      // The same Apply action waits for acceptance to finish the workflow.
      const deadline = Date.now() + 60000;
      while (current.status === "accepted" && !durableChangeSetCanCommit(adaptDurableChangeSet(current, panelId), canCommit)
        && !["failed", "cancelled", "timed_out"].includes(current.workflow_status || "") && Date.now() < deadline) {
        await new Promise(resolve => window.setTimeout(resolve, 1500));
        if (generation.current !== actionGeneration) return;
        current = await present();
      }
      if (current.status === "accepted") {
        if (!durableChangeSetCanCommit(adaptDurableChangeSet(current, panelId), canCommit)) {
          // Acceptance is persisted. Polling resumes the same candidate after
          // the workflow finishes; never claim that it was committed early.
          return;
        }
        try { await commitDurableChangeSet(id); }
        catch (reason) {
          current = await present();
          if (current.status !== "committed") throw reason;
        }
        current = await present();
      }
      if (generation.current !== actionGeneration) return;
      if (current.status !== "committed") throw new Error("此候选当前不能应用，请检查审查状态和版本基线。");
      await onReviewed?.("committed");
    } catch (reason) {
      if (generation.current !== actionGeneration) return;
      // A successful accept is never undone locally when commit or its response
      // fails. Query the same candidate; the next click resumes its actual state.
      try { await present(); } catch { /* keep the last server-confirmed state */ }
      if (generation.current === actionGeneration) setError(reason instanceof Error ? reason.message : "应用结果尚待核对，请重试同一候选");
    } finally { if (generation.current === actionGeneration) setRestoring(false); }
  };

  return (
    <WorkspaceDialog
      description="仅展示版本快照、任务结果、参数、几何指标、文件引用和验证记录能够证实的变化；缺失证据保持未知。"
      footer={(
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="mr-auto type-caption text-[var(--faint)]">
            {changeSet?.source === "durable"
              ? `审查状态：${changeSet.reviewStatus || "未知"}`
              : "当前为兼容快照审查；持久 Change Set 建立后可执行接受与提交。"}
          </span>
          {changeSet?.reviewStatus === "pending_review" && acceptanceHint ? (
            <span className="mr-auto type-caption text-amber-700" id="change-set-acceptance-hint" role="status">
              {acceptanceHint}
            </span>
          ) : null}
          {workflowStillRunning ? (
            <span className="mr-auto type-caption text-amber-700" role="status">
              工作流仍在完成验证，完成后可提交版本。
            </span>
          ) : null}
          <button className="workspace-button" onClick={onClose} type="button">关闭</button>
          <button
            className="workspace-button"
            disabled={
              !changeSet
              || (changeSet?.source !== "durable" && !onAskAgent)
              || restoring
              || (changeSet?.source === "durable" && detail?.can_review !== true)
              || (
                changeSet.source === "durable"
                && changeSet.reviewStatus !== "pending_review"
              )
            }
            onClick={() => void requestModification()}
            type="button"
          >
            请求修改
          </button>
          <button
            className="workspace-button"
            disabled={
              !changeSet?.baseRevisionId
              || (changeSet?.source === "durable" && (changeSet.reviewStatus === "committed" ? detail?.can_rollback !== true : detail?.can_review !== true))
              || (changeSet?.source !== "durable" && !onRestore)
              || restoring
              || (
                changeSet.source === "durable"
                && !["pending_review", "committed"].includes(
                  changeSet.reviewStatus || "",
                )
              )
            }
            onClick={() => void rollback()}
            type="button"
          >
            {restoring
              ? "正在处理"
              : changeSet?.source === "durable"
                && changeSet.reviewStatus !== "committed"
                ? "拒绝变更"
                : "回滚"}
          </button>
          {detail?.can_review && !(detail.can_commit && canCommit) ? <button
            aria-describedby={acceptanceHint ? "change-set-acceptance-hint" : undefined}
            className="workspace-button"
            disabled={!acceptanceReady || restoring}
            onClick={() => void accept()}
            type="button"
          >
            {changeSet?.validation.status === "warning" ? "接受并确认已审阅风险" : "接受变更"}
          </button> : null}
          {detail?.can_commit && canCommit ? <button className="workspace-button workspace-button--primary" type="button"
            disabled={restoring || !["pending_review", "accepted", "committed"].includes(detail.status)
              || (detail.status === "accepted" && !commitReady)
              || (detail.status !== "committed" && (!detail.base_is_current || changeSet?.validation.status === "fail" || changeSet?.validation.status === "unknown"))
              || (detail.status === "pending_review" && (!detail.can_review || (changeSet?.validation.status === "warning" && !reviewNote.trim())))}
            onClick={() => void apply()}>{restoring ? "正在核对并应用" : detail.status === "committed" ? "同步已提交版本" : "应用修改"}</button> : null}
        </div>
      )}
      onClose={onClose}
      open={open}
      title="变更审查"
    >
      <div className="space-y-5 p-5">
        {detail ? <div className="rounded border border-[var(--line)] p-3 type-caption" data-testid="candidate-base">
          <details><summary>版本技术详情</summary><p>候选 {detail.candidate_revision_id.slice(0,8)} · 基线 {detail.base_revision_id.slice(0,8)} / {detail.base_state_version === null ? "代次未知" : `v${detail.base_state_version}`}</p>
          <p>当前 Head {detail.head_revision_id?.slice(0,8)} / v{detail.head_state_version}</p></details>
          {detail.status === "accepted" ? <p role="status">已接受，尚未提交。重试将继续提交同一候选。</p> : null}
          {detail.status === "committed" ? <p role="status">服务器已提交；工作区以同步后的文档 Head 为准。</p> : null}
          {!detail.base_is_current && ["pending_review", "accepted"].includes(detail.status) ? <p role="alert">候选基线或合并来源已过期，请基于当前版本重新确认。</p> : null}
          {detail.merge_source ? <p>合并来源 {detail.merge_source.source_revision_id.slice(0,8)} / v{detail.merge_source.source_state_version}；来源当前 {detail.merge_source.source_head_revision_id.slice(0,8)} / v{detail.merge_source.source_head_state_version}</p> : null}
        </div> : null}
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
            {changeSet.source === "durable" ? (
              <label className="block type-control text-[var(--muted)]">
                审查意见
                <textarea
                  className="mt-2 min-h-20 w-full resize-y rounded-lg border border-[var(--line)] bg-white px-3 py-2 type-control text-[var(--ink)] outline-none focus:border-[var(--accent)]"
                  maxLength={4000}
                  onChange={(event) => setReviewNote(event.target.value)}
                  placeholder={changeSet.validation.status === "warning" ? "存在未通过或未能判定项；接受前请填写审阅意见。" : "拒绝或请求修改时必填；接受时可选。"}
                  value={reviewNote}
                />
              </label>
            ) : null}
            <section className="border-b border-[var(--line)] pb-4">
              <div className="grid gap-3 type-body sm:grid-cols-3">
                <div><p className="text-[var(--faint)]">修改目标</p><p className="mt-1 text-[var(--ink)]">{changeSet.objective || "未记录"}</p></div>
                <div><p className="text-[var(--faint)]">修订序号</p><p className="mt-1 text-[var(--ink)]">{changeSet.baseVersion ? `#${changeSet.baseVersion} → #${changeSet.targetVersion}` : `父修订未知 → #${changeSet.targetVersion}`}</p></div>
                <div><p className="text-[var(--faint)]">修改对象数</p><p className="mt-1 text-[var(--ink)]">{changeSet.modifiedObjectCount ?? "未知"}</p></div>
              </div>
              <p className="mt-3 break-all type-caption text-[var(--faint)]">
                任务 {changeSet.taskId || "未记录"} · 请求 {changeSet.requestId || "未记录"}
              </p>
            </section>

            <section>
              <h3 className="type-section-heading  text-[var(--ink)]">参数变化</h3>
              {changeSet.parameterChanges.length ? (
                <div className="mt-2 overflow-hidden rounded-lg border border-[var(--line)]">
                  {changeSet.parameterChanges.map((change) => (
                    <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-[var(--line)] px-3 py-2 type-body last:border-b-0" key={change.parameterId}>
                      <span className="truncate text-[var(--ink)]">{change.label}</span>
                      <span className="text-[var(--muted)]">{change.before} → {change.after}{change.unit || ""}</span>
                    </div>
                  ))}
                </div>
              ) : <p className="mt-2 type-body text-[var(--faint)]">没有父版本参数证据，或已记录参数值未变化。</p>}
            </section>

            <section className="grid gap-4 sm:grid-cols-2">
              <div>
                <h3 className="type-section-heading  text-[var(--ink)]">代码与几何</h3>
                <dl className="mt-2 space-y-2 type-body text-[var(--muted)]">
                  <div className="flex justify-between gap-3"><dt>代码</dt><dd>{statusLabel(changeSet.code.status)}</dd></div>
                  <div className="flex justify-between gap-3"><dt>几何</dt><dd>{statusLabel(changeSet.geometry.status)}</dd></div>
                  {changeSet.geometry.metrics.map((metric) => (
                    <div className="flex justify-between gap-3" key={metric.label}><dt>{metric.label}</dt><dd>{metric.before} → {metric.after} {metric.unit}</dd></div>
                  ))}
                </dl>
              </div>
              <div>
                <h3 className="type-section-heading  text-[var(--ink)]">验证与风险</h3>
                <p className="mt-2 type-body text-[var(--muted)]">验证：{validationLabel(changeSet.validation.status)} · {changeSet.validation.summary}</p>
                {changeSet.validation.gates?.length ? (
                  <ul className="mt-2 space-y-1 type-caption text-[var(--faint)]">
                    {changeSet.validation.gates.map((gate) => (
                      <li key={gate.evidenceId || `${gate.gate}:${gate.outcome}`} title={gate.evidenceHash}>
                        · {gate.gate.toUpperCase()} · {gate.mode === "required" ? "必需" : "建议"} · {gate.outcome === "passed" ? "通过" : gate.outcome === "failed" ? "存在问题" : "未能判定"}
                      </li>
                    ))}
                  </ul>
                ) : null}
                <p className="mt-2 type-body text-[var(--muted)]">风险：{riskLabel(changeSet.risk.level)}</p>
                <ul className="mt-1 space-y-1 type-caption text-[var(--faint)]">
                  {changeSet.risk.reasons.map((reason) => <li key={reason}>· {reason}</li>)}
                </ul>
              </div>
            </section>

            <section>
              <h3 className="type-section-heading  text-[var(--ink)]">文件变化</h3>
              {changeSet.files.length ? (
                <div className="mt-2 space-y-2">
                  {changeSet.files.map((file, index) => (
                    <div className="rounded-lg border border-[var(--line)] px-3 py-2 type-body" key={`${file.format}:${file.kind}:${index}`}>
                      <p className="text-[var(--ink)]">{file.format} · {fileKindLabel(file.kind)}</p>
                      <p className="mt-1 break-all type-caption text-[var(--faint)]">
                        {file.evidence === "sha256"
                          ? "文件内容变化已由不可变 Artifact 的 SHA-256 证实。"
                          : "仅能证实文件引用变化；当前元数据没有 SHA-256，不能声称内容已变化。"}
                      </p>
                    </div>
                  ))}
                </div>
              ) : <p className="mt-2 type-body text-[var(--faint)]">没有可证实的文件引用变化。</p>}
            </section>

            <section>
              <h3 className="type-section-heading  text-[var(--ink)]">Agent 操作日志</h3>
              {changeSet.agentLogs.length ? (
                <ol className="mt-2 space-y-1 type-body text-[var(--muted)]">
                  {changeSet.agentLogs.map((log) => <li key={log}>{log}</li>)}
                </ol>
              ) : <p className="mt-2 type-body text-[var(--faint)]">没有已持久化的 Agent 操作日志。</p>}
            </section>
          </>
        ) : null}
      </div>
    </WorkspaceDialog>
  );
}
