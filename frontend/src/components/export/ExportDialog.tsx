import { useCallback, useEffect, useMemo, useState } from "react";
import {
  downloadEngineeringArtifact,
  getOnshapeConfig,
  listOnshapeLinks,
  publishToOnshape,
  refreshOnshapeLink,
} from "../../services/engineeringService";
import type { AsyncStatus, ExportJob, OnshapeConfig, OnshapeLink } from "../../types/engineering";
import { InlineState, WorkspaceDialog } from "../common/WorkspaceOverlay";
import { Icon } from "../ui/Icon";

function filenameFromUrl(url: string, fallback: string): string {
  const encoded = url.split("/").pop()?.split("?")[0];
  if (!encoded) return fallback;
  try {
    return decodeURIComponent(encoded);
  } catch {
    return fallback;
  }
}

function onshapeStatusLabel(status: string): string {
  const normalized = status.trim().toLowerCase();
  if (normalized === "done") return "导入完成";
  if (["failed", "cancelled", "canceled"].includes(normalized)) return "导入失败";
  if (["active", "working", "processing"].includes(normalized)) return "正在导入";
  return "已提交";
}

interface ExportDialogProps {
  open: boolean;
  jobs: ExportJob[];
  requestId?: string | null;
  onClose: () => void;
}

export default function ExportDialog({ open, jobs, requestId, onClose }: ExportDialogProps) {
  const [active, setActive] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [onshapeStatus, setOnshapeStatus] = useState<AsyncStatus>("idle");
  const [onshapeConfig, setOnshapeConfig] = useState<OnshapeConfig | null>(null);
  const [onshapeLink, setOnshapeLink] = useState<OnshapeLink | null>(null);
  const [onshapeError, setOnshapeError] = useState<string | null>(null);
  const [onshapeAction, setOnshapeAction] = useState<"publish" | "refresh" | null>(null);
  const stepArtifact = useMemo(
    () => jobs.find((job) => job.format === "STEP" && job.status === "available")?.artifact ?? null,
    [jobs],
  );

  const loadOnshape = useCallback(async () => {
    setOnshapeStatus("loading");
    setOnshapeError(null);
    try {
      const config = await getOnshapeConfig();
      setOnshapeConfig(config);
      if (config.configured && requestId) {
        const links = await listOnshapeLinks(requestId);
        setOnshapeLink(links[0] ?? null);
      } else {
        setOnshapeLink(null);
      }
      setOnshapeStatus("success");
    } catch (reason) {
      setOnshapeStatus("error");
      setOnshapeError(reason instanceof Error ? reason.message : "Onshape 状态加载失败");
    }
  }, [requestId]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void loadOnshape(), 0);
    return () => window.clearTimeout(timer);
  }, [loadOnshape, open]);

  const download = async (job: ExportJob) => {
    if (!job.artifact) return;
    setActive(job.id);
    setDownloadError(null);
    try {
      await downloadEngineeringArtifact(job.artifact.url, job.artifact.name);
    } catch (reason) {
      setDownloadError(reason instanceof Error ? reason.message : "文件下载失败");
    } finally {
      setActive(null);
    }
  };

  const publish = async () => {
    if (!requestId || !stepArtifact) return;
    setOnshapeAction("publish");
    setOnshapeError(null);
    try {
      const filename = filenameFromUrl(stepArtifact.url, stepArtifact.name);
      setOnshapeLink(await publishToOnshape(requestId, filename));
    } catch (reason) {
      setOnshapeError(reason instanceof Error ? reason.message : "发布到 Onshape 失败");
    } finally {
      setOnshapeAction(null);
    }
  };

  const refresh = async () => {
    if (!requestId || !onshapeLink?.translation_id) return;
    setOnshapeAction("refresh");
    setOnshapeError(null);
    try {
      setOnshapeLink(await refreshOnshapeLink(requestId));
    } catch (reason) {
      setOnshapeError(reason instanceof Error ? reason.message : "Onshape 状态刷新失败");
    } finally {
      setOnshapeAction(null);
    }
  };

  const canPublish = Boolean(onshapeConfig?.configured && requestId && stepArtifact);

  return (
    <WorkspaceDialog
      description="只显示本次生成结果实际提供的格式；不可用格式不会创建假下载。"
      footer={<div className="flex justify-end"><button className="workspace-button" onClick={onClose} type="button">完成</button></div>}
      onClose={onClose}
      open={open}
      title="导出工程产物"
    >
      <div className="space-y-5 p-5">
        <section>
          <h3 className="mb-2 type-section-heading  text-[var(--muted)]">本地产物</h3>
          {downloadError ? <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 type-body text-red-800" role="alert">{downloadError}</div> : null}
          {jobs.length ? (
            <div className="divide-y divide-[var(--line)] overflow-hidden rounded-lg border border-[var(--line)]">
              {jobs.map((job) => (
                <div className="flex min-h-[66px] items-center gap-3 px-4 py-3" key={job.id}>
                  <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--subtle)] text-[var(--muted)]"><Icon name={job.format === "ZIP" ? "layers" : "file"} size={17} /></span>
                  <div className="min-w-0 flex-1"><p className="type-body  text-[var(--ink)]">{job.label}</p><p className="mt-0.5 truncate type-caption text-[var(--faint)]">{job.artifact?.name || job.limitation}</p></div>
                  <button className={`workspace-button ${job.status === "available" ? "workspace-button--primary" : ""}`} disabled={job.status !== "available" || active === job.id} onClick={() => void download(job)} type="button">{active === job.id ? "下载中" : job.status === "available" ? "下载" : "不可用"}</button>
                </div>
              ))}
            </div>
          ) : <InlineState detail="完成一次成功生成后，这里只会列出后端实际返回的文件。" title="暂无可导出产物" />}
        </section>

        <section>
          <div className="mb-2 flex items-center justify-between gap-3">
            <div><h3 className="type-section-heading  text-[var(--muted)]">Onshape 云端发布</h3><p className="mt-1 type-caption text-[var(--faint)]">将当前真实 STEP 文件提交到后端配置的 Onshape 账号。</p></div>
            {onshapeStatus === "success" && canPublish ? <button className="workspace-button workspace-button--primary" disabled={onshapeAction !== null} onClick={() => void publish()} type="button">{onshapeAction === "publish" ? "发布中" : onshapeLink ? "重新发布" : "发布"}</button> : null}
          </div>
          <div className="rounded-lg border border-[var(--line)] p-4">
            {onshapeStatus === "idle" || onshapeStatus === "loading" ? <InlineState detail="正在读取后端配置和当前发布记录。" title="检查 Onshape 状态" /> : null}
            {onshapeStatus === "error" ? <InlineState actionLabel="重试" detail={onshapeError || "无法读取后端状态。"} onAction={() => void loadOnshape()} title="Onshape 状态加载失败" tone="error" /> : null}
            {onshapeStatus === "success" && !onshapeConfig?.configured ? <InlineState detail="后端尚未设置 ONSHAPE_ACCESS_KEY 与 ONSHAPE_SECRET_KEY，发布功能不会执行。" title="Onshape 未配置" /> : null}
            {onshapeStatus === "success" && onshapeConfig?.configured && !stepArtifact ? <InlineState detail="当前生成结果没有 STEP 文件，因此不能发布到 Onshape。" title="缺少 STEP 产物" /> : null}
            {onshapeStatus === "success" && onshapeConfig?.configured && stepArtifact && !requestId ? <InlineState detail="当前结果缺少后端 request_id，无法建立发布记录。" title="缺少请求标识" tone="error" /> : null}
            {onshapeStatus === "success" && canPublish && !onshapeLink ? <InlineState detail={`将发布 ${stepArtifact?.name || "STEP 文件"}；新文档默认${onshapeConfig?.default_document_public ? "按部署配置公开" : "保持私有"}。`} title="尚未发布" /> : null}
            {onshapeStatus === "success" && onshapeLink ? (
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-[var(--agent-soft)] text-[var(--agent)]"><Icon name="box" size={18} /></span>
                <div className="min-w-0 flex-1">
                  <p className="truncate type-body  text-[var(--ink)]">{onshapeLink.document_name || "Onshape 文档"}</p>
                  <p className="mt-1 type-body text-[var(--muted)]">{onshapeStatusLabel(onshapeLink.status)} · {onshapeLink.step_filename || stepArtifact?.name}</p>
                </div>
                <div className="flex gap-2">
                  {onshapeLink.translation_id ? <button className="workspace-button" disabled={onshapeAction !== null} onClick={() => void refresh()} type="button">{onshapeAction === "refresh" ? "刷新中" : "刷新状态"}</button> : null}
                  <a className="workspace-button workspace-button--primary" href={onshapeLink.onshape_url} rel="noopener noreferrer" target="_blank">打开 Onshape</a>
                </div>
              </div>
            ) : null}
            {onshapeError && onshapeStatus === "success" ? <p className="mt-3 type-body text-red-700" role="alert">{onshapeError}</p> : null}
          </div>
        </section>
      </div>
    </WorkspaceDialog>
  );
}
