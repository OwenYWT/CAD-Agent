import { lazy, Suspense, useState } from "react";
import type { GenerationResult, StepUpdate } from "../../types";
import type { DocumentViewIdentity } from "../../types/document";
import { viewLabel } from "../../adapters/documentView";
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
  taskLabel?:string;taskPhase?:string;hasParameters?:boolean;
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
  taskLabel,taskPhase,hasParameters=true,
  onBack,
}: MechanicalWorkspaceProps) {
  const [viewerKey, setViewerKey] = useState(0);
  const stlUrl = viewedMeshUrl || result?.files?.stl || null;
  const svgUrl = result?.files?.svg || null;
  const is2D = Boolean(svgUrl && !stlUrl);
  const files = Object.entries(result?.files || {});
  const activeUrl = stlUrl || svgUrl || files[0]?.[1] || "";
  const activeFilename = nativeDocumentId ? "原生 CAD 模型" : activeUrl ? "几何预览" : "等待生成";


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
        </div>
      </header>
      <div className="ww-viewer-canvas">
        <div className="absolute left-3 top-3 z-10 flex gap-2">
          {identity ? <span data-testid="view-identity" data-revision={identity.viewedRevisionId} data-mode={identity.mode} className="workspace-chip bg-white">{result?.success || hasParameters ? identity.mode === "committed" ? `已保存版本 · v${identity.headStateVersion}` : identity.mode === "candidate" ? "AI 候选 · 待确认" : "历史版本 · 只读" : "空文档，尚无模型"}</span> : null}
          <details className="workspace-chip bg-white"><summary>版本来源</summary><p>{viewLabel(identity || null)}</p><p>{result?.request_id ? `生成此版本的任务：${result.request_id}` : "尚无模型版本"}</p></details>
          {result?.validation?.is_watertight ? <span className="workspace-chip bg-white text-emerald-700">已闭合</span> : null}
        </div>
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

      </div>
      <footer className="ww-viewer-statusbar">
        <span>{is2D ? "SVG 预览" : nativeDocumentId ? "部件场景" : "STL 预览"}</span>
        <span>{files.length ? `${files.length} 个工程文件` : "尚无工程文件"}</span>
        <span className="ml-auto" data-testid="viewer-task-state" data-task-phase={taskPhase}>{taskLabel || (result?.validation?.is_watertight ? "模型已闭合" : result?.success ? "模型已生成" : isGenerating ? "正在生成" : "等待任务")}</span>
      </footer>
    </section>
  );
}
