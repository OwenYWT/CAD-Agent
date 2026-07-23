import { lazy, Suspense, useState } from "react";
import type { GenerationResult } from "../../types";
import { Icon } from "../ui/Icon";
import Viewer2D from "../Viewer2D";

const Viewer3D = lazy(() => import("../Viewer3D"));

interface MechanicalWorkspaceProps {
  result: GenerationResult | null;
  onBack: () => void;
  onProperties: () => void;
  onAgent: () => void;
}

export default function MechanicalWorkspace({ result, onBack, onProperties, onAgent }: MechanicalWorkspaceProps) {
  const [viewerKey, setViewerKey] = useState(0);
  const [toolMessage, setToolMessage] = useState<string | null>(null);
  const stlUrl = result?.files?.stl || null;
  const svgUrl = result?.files?.svg || null;
  const is2D = Boolean(svgUrl && !stlUrl);

  const unavailable = (label: string) => setToolMessage(`${label}需要装配层级或 B-Rep 几何；当前 STL/SVG 预览不支持，未执行假操作。`);

  return (
    <section className="flex h-full min-h-0 flex-col bg-white">
      <header className="flex min-h-[46px] shrink-0 items-center gap-2 overflow-x-auto border-b border-[var(--line)] px-3 sm:px-4">
        <button className="workspace-button workspace-button--ghost shrink-0" onClick={onBack} type="button">← 返回项目流程</button>
        <h1 className="shrink-0 text-sm font-semibold text-[var(--ink)]">机械设计</h1>
        <span className="hidden text-xs text-[var(--faint)] md:inline">当前 CAD 生成结果</span>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <button className="workspace-button workspace-button--ghost" onClick={() => setViewerKey((key) => key + 1)} type="button"><Icon name="rotate" size={14} />适应视图</button>
          <button className="workspace-button workspace-button--ghost" onClick={() => unavailable("测量")} type="button">测量</button>
          <button className="workspace-button workspace-button--ghost" onClick={() => unavailable("爆炸视图")} type="button">爆炸</button>
          <button className="workspace-button workspace-button--ghost" onClick={() => unavailable("剖切")} type="button">剖切</button>
          <button className="workspace-button" onClick={onProperties} type="button"><Icon name="settings" size={14} />属性</button>
        </div>
      </header>
      <div className="relative min-h-0 flex-1 bg-[#f4f4f1]">
        <div className="absolute left-3 top-3 z-10 flex gap-2"><span className="workspace-chip bg-white">{result?.request_id ? `任务 ${result.request_id.slice(0, 8)}` : "尚无模型"}</span>{result?.validation?.is_watertight ? <span className="workspace-chip bg-white text-emerald-700">网格闭合</span> : null}</div>
        {toolMessage ? <div className="absolute left-1/2 top-16 z-20 w-[min(92%,520px)] -translate-x-1/2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900 shadow-lg" role="status">{toolMessage}<button className="ml-3 underline" onClick={() => setToolMessage(null)} type="button">关闭</button></div> : null}
        {is2D ? <Viewer2D key={svgUrl} svgUrl={svgUrl} /> : <Suspense fallback={<div className="grid h-full place-items-center text-sm text-[var(--muted)]">正在载入 Three.js 工作区…</div>}><Viewer3D key={`${stlUrl}:${viewerKey}`} stlUrl={stlUrl} /></Suspense>}
        {!result?.success ? <div className="pointer-events-none absolute inset-x-0 bottom-7 flex justify-center"><button className="workspace-button pointer-events-auto" onClick={onAgent} type="button">询问 Agent 如何开始</button></div> : null}
      </div>
    </section>
  );
}
