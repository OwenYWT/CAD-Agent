import { lazy, Suspense, useState } from "react";
import type { GenerationResult, StepUpdate } from "../../types";
import type { DocumentViewIdentity } from "../../types/document";
import { viewLabel } from "../../adapters/documentView";
import { engineeringTaskEventLabel } from "../../utils/engineeringLabels";
import { Icon } from "../ui/Icon";
import Viewer2D from "../Viewer2D";

const Viewer3D = lazy(() => import("../Viewer3D"));
const DocumentSceneViewer = lazy(() => import("./DocumentSceneViewer"));

interface MechanicalWorkspaceProps {
  viewedMeshUrl?: string;
  identity?: DocumentViewIdentity | null;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  onCommitted?: () => void;
  onCandidate?: () => void;
  viewError?: string;
  nativeDocumentId?: string;
  result: GenerationResult | null;
  isGenerating: boolean;
  currentStep: StepUpdate | null;
  onBack: () => void;
  onProperties: () => void;
  onAgent: () => void;
  onInspector?: () => void;
}

export default function MechanicalWorkspace({
  viewedMeshUrl,
  identity,
  selectedId,
  onSelect,
  onCommitted,
  onCandidate,
  viewError,
  nativeDocumentId,
  result,
  isGenerating,
  currentStep,
  onBack,
  onProperties,
  onAgent,
  onInspector,
}: MechanicalWorkspaceProps) {
  const [viewerKey, setViewerKey] = useState(0);
  const stlUrl = viewedMeshUrl || result?.files?.stl || null;
  const svgUrl = result?.files?.svg || null;
  const is2D = Boolean(svgUrl && !stlUrl);
  const files = Object.entries(result?.files || {});
  const activeUrl = stlUrl || svgUrl || files[0]?.[1] || "";
  const activeFilename = activeUrl ? decodeURIComponent(activeUrl.split("/").pop()?.split("?")[0] || "工程模型") : "等待生成";


  return (
    <section className="ww-viewer-shell">
      <header className="ww-viewer-toolbar">
        <button aria-label="打开项目流程" className="workspace-icon-button shrink-0" onClick={onBack} title="项目流程" type="button"><Icon name="clock" size={15} /></button>
        <div className="ww-viewer-title"><strong>模型</strong><span>{activeFilename}</span></div>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          {identity && identity.mode !== "committed" && onCommitted ? <button className="workspace-button" type="button" onClick={onCommitted}>查看已提交版本</button> : null}
          {onCandidate ? <button className="workspace-button" type="button" onClick={onCandidate}>预览候选变更</button> : null}
          <span className="ww-viewer-hint hidden 2xl:inline">拖动旋转 · 滚轮缩放 · 右键平移</span>
          <button aria-label="适应视图" className="workspace-icon-button" onClick={() => setViewerKey((key) => key + 1)} title="适应视图" type="button"><Icon name="rotate" size={14} /></button>
          <button className="workspace-button" onClick={onProperties} type="button"><Icon name="sliders" size={14} />参数</button>
          {onInspector ? <button className="workspace-button xl:hidden" onClick={onInspector} type="button"><Icon name="sliders" size={14} />检查器</button> : null}
        </div>
      </header>
      <div className="ww-viewer-canvas">
        <div className="absolute left-3 top-3 z-10 flex gap-2">
          {identity ? <span data-testid="view-identity" data-revision={identity.viewedRevisionId} data-mode={identity.mode} className="workspace-chip bg-white">{viewLabel(identity)}</span> : null}
          <span className="workspace-chip bg-white">{result?.request_id ? `任务 ${result.request_id.slice(0, 8)}` : "等待生成"}</span>
          {result?.validation?.is_watertight ? <span className="workspace-chip bg-white text-emerald-700">已闭合</span> : null}
        </div>
        {isGenerating ? (
          <div className="absolute left-1/2 top-4 z-20 flex w-[min(92%,520px)] -translate-x-1/2 items-center gap-3 rounded-lg border border-[var(--agent-border)] bg-[var(--agent-soft)] px-4 py-3 text-[var(--ink)] shadow-lg" role="status">
            <span className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-[var(--agent)] border-t-transparent" />
            <div className="min-w-0">
              <p className="type-body ">正在重新计算模型</p>
              <p className="mt-0.5 truncate type-caption text-[var(--agent)]">{engineeringTaskEventLabel(currentStep?.message || "正在等待执行进度")}</p>
            </div>
          </div>
        ) : null}
        {viewError ? <p role="alert" className="absolute inset-x-4 bottom-12 z-20 rounded bg-red-50 p-3 text-red-700">{viewError}</p> : null}
        <div className="absolute inset-0 min-h-0 min-w-0">
          {is2D ? (
            <Viewer2D key={svgUrl} svgUrl={svgUrl} />
          ) : (
            <Suspense fallback={<div className="grid h-full place-items-center type-body text-[var(--muted)]">{String.fromCharCode(0x6b63, 0x5728, 0x52a0, 0x8f7d, 0x20, 0x33, 0x44, 0x20, 0x6a21, 0x578b, 0x2e, 0x2e, 0x2e)}</div>}>
              {nativeDocumentId && identity ? <DocumentSceneViewer key={`${nativeDocumentId}:${identity.viewedRevisionId}:${viewerKey}`} documentId={nativeDocumentId} revisionId={identity.viewedRevisionId} selectedId={selectedId} onSelect={onSelect} />
                : <Viewer3D key={`${stlUrl}:${viewerKey}`} stlUrl={stlUrl} />}
            </Suspense>
          )}
        </div>
        {!result?.success ? (
          <div className="pointer-events-none absolute inset-x-0 bottom-7 flex justify-center">
            <button className="workspace-button pointer-events-auto" onClick={onAgent} type="button">{String.fromCharCode(0x8bf7, 0x6c42, 0x20, 0x41, 0x67, 0x65, 0x6e, 0x74, 0x20, 0x534f, 0x52a9)}</button>
          </div>
        ) : null}
      </div>
      <footer className="ww-viewer-statusbar">
        <span>{is2D ? "SVG 预览" : nativeDocumentId ? "部件场景" : "STL 预览"}</span>
        <span>{files.length ? `${files.length} 个工程文件` : "尚无工程文件"}</span>
        <span className="ml-auto">{result?.validation?.is_watertight ? "模型已闭合" : result?.success ? "模型已生成" : isGenerating ? "正在生成" : "等待任务"}</span>
      </footer>
    </section>
  );
}
