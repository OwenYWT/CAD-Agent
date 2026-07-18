import { useState } from "react";
import { downloadEngineeringArtifact } from "../../services/engineeringService";
import type { ExportJob } from "../../types/engineering";
import { InlineState, WorkspaceDialog } from "../common/WorkspaceOverlay";
import { Icon } from "../ui/Icon";

export default function ExportDialog({ open, jobs, onClose }: { open: boolean; jobs: ExportJob[]; onClose: () => void }) {
  const [active, setActive] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const download = async (job: ExportJob) => {
    if (!job.artifact) return;
    setActive(job.id); setError(null);
    try { await downloadEngineeringArtifact(job.artifact.url, job.artifact.name); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "文件下载失败"); }
    finally { setActive(null); }
  };
  return (
    <WorkspaceDialog description="只显示本次生成结果实际提供的格式；不可用格式不会创建假下载。" footer={<div className="flex justify-end"><button className="workspace-button" onClick={onClose} type="button">完成</button></div>} onClose={onClose} open={open} title="导出工程产物">
      <div className="p-5">
        {error ? <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800" role="alert">{error}</div> : null}
        {jobs.length ? <div className="divide-y divide-[var(--line)] overflow-hidden rounded-lg border border-[var(--line)]">{jobs.map((job) => <div className="flex min-h-[66px] items-center gap-3 px-4 py-3" key={job.id}><span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--subtle)] text-[var(--muted)]"><Icon name={job.format === "ZIP" ? "layers" : "file"} size={17} /></span><div className="min-w-0 flex-1"><p className="text-sm font-medium text-[var(--ink)]">{job.label}</p><p className="mt-0.5 truncate text-[11px] text-[var(--faint)]">{job.artifact?.name || job.limitation}</p></div><button className={`workspace-button ${job.status === "available" ? "workspace-button--primary" : ""}`} disabled={job.status !== "available" || active === job.id} onClick={() => void download(job)} type="button">{active === job.id ? "下载中" : job.status === "available" ? "下载" : "不可用"}</button></div>)}</div> : <InlineState detail="完成一次成功生成后，这里只会列出后端实际返回的文件。" title="暂无可导出产物" />}
      </div>
    </WorkspaceDialog>
  );
}
