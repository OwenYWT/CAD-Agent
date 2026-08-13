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
  const stlUrl = result?.files?.stl || null;
  const svgUrl = result?.files?.svg || null;
  const is2D = Boolean(svgUrl && !stlUrl);


  return (
    <section className="flex h-full min-h-0 min-w-0 flex-1 flex-col bg-white">
      <header className="flex min-h-[46px] shrink-0 items-center gap-2 overflow-x-auto border-b border-[var(--line)] px-3 sm:px-4">
        <button className="workspace-button workspace-button--ghost shrink-0" onClick={onBack} type="button">{String.fromCharCode(0x8fd4, 0x56de, 0x9879, 0x76ee)}</button>
        <h1 className="shrink-0 text-sm font-semibold text-[var(--ink)]">{String.fromCharCode(0x673a, 0x68b0, 0x8bbe, 0x8ba1)}</h1>
        <span className="hidden text-xs text-[var(--faint)] md:inline">{String.fromCharCode(0x53, 0x54, 0x45, 0x50, 0x20, 0x2f, 0x20, 0x53, 0x54, 0x4c, 0x20, 0x2f, 0x20, 0x53, 0x56, 0x47, 0x20, 0x9884, 0x89c8)}</span>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <button className="workspace-button workspace-button--ghost" onClick={() => setViewerKey((key) => key + 1)} type="button"><Icon name="rotate" size={14} />{String.fromCharCode(0x5237, 0x65b0, 0x89c6, 0x56fe)}</button>
          <button className="workspace-button" onClick={onProperties} type="button"><Icon name="settings" size={14} />{String.fromCharCode(0x5c5e, 0x6027)}</button>
        </div>
      </header>
      <div className="relative min-h-0 flex-1 overflow-hidden bg-[#f4f4f1]">
        <div className="absolute left-3 top-3 z-10 flex gap-2">
          <span className="workspace-chip bg-white">{result?.request_id ? `${String.fromCharCode(0x4efb, 0x52a1)} ${result.request_id.slice(0, 8)}` : String.fromCharCode(0x7b49, 0x5f85, 0x751f, 0x6210)}</span>
          {result?.validation?.is_watertight ? <span className="workspace-chip bg-white text-emerald-700">{String.fromCharCode(0x5df2, 0x95ed, 0x5408)}</span> : null}
        </div>
        <div className="absolute inset-0 min-h-0 min-w-0">
          {is2D ? (
            <Viewer2D key={svgUrl} svgUrl={svgUrl} />
          ) : (
            <Suspense fallback={<div className="grid h-full place-items-center text-sm text-[var(--muted)]">{String.fromCharCode(0x6b63, 0x5728, 0x52a0, 0x8f7d, 0x20, 0x33, 0x44, 0x20, 0x6a21, 0x578b, 0x2e, 0x2e, 0x2e)}</div>}>
              <Viewer3D key={`${stlUrl}:${viewerKey}`} stlUrl={stlUrl} />
            </Suspense>
          )}
        </div>
        {!result?.success ? (
          <div className="pointer-events-none absolute inset-x-0 bottom-7 flex justify-center">
            <button className="workspace-button pointer-events-auto" onClick={onAgent} type="button">{String.fromCharCode(0x8bf7, 0x6c42, 0x20, 0x41, 0x67, 0x65, 0x6e, 0x74, 0x20, 0x534f, 0x52a9)}</button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
