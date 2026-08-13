import { useEffect, useState } from "react";
import { authFetch } from "../auth";
import { Icon } from "./ui/Icon";

const API_BASE = import.meta.env.VITE_API_BASE || "";

function sanitizeSvg(raw: string): string {
  let cleaned = raw.replace(/<script[\s\S]*?<\/script>/gi, "");
  cleaned = cleaned.replace(/on\w+\s*=\s*"[^"]*"/gi, "");
  cleaned = cleaned.replace(/on\w+\s*=\s*'[^']*'/gi, "");
  cleaned = cleaned.replace(/<iframe[\s\S]*?(<\/iframe>|\/?>)/gi, "");
  cleaned = cleaned.replace(/<object[\s\S]*?(<\/object>|\/?>)/gi, "");
  cleaned = cleaned.replace(/<embed[\s\S]*?\/?>/gi, "");
  return cleaned.replace(/javascript\s*:/gi, "");
}

function LoadedSvg({ svgUrl }: { svgUrl: string }) {
  const [state, setState] = useState<{ content: string | null; error: boolean }>({ content: null, error: false });
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    const controller = new AbortController();
    const target = /^https?:\/\//.test(svgUrl) ? svgUrl : `${API_BASE}${svgUrl}`;
    authFetch(target, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then((text) => setState({ content: sanitizeSvg(text), error: false }))
      .catch((error) => {
        if (error instanceof Error && error.name !== "AbortError") setState({ content: null, error: true });
      });
    return () => controller.abort();
  }, [svgUrl]);

  return (
    <div className="relative h-full w-full overflow-hidden bg-white">
      <div className="absolute right-3 top-3 z-10 flex gap-1 rounded-md border border-slate-200 bg-white p-1 shadow-sm">
        <button aria-label="放大二维图" className="icon-button" onClick={() => setZoom((value) => Math.min(value * 1.25, 5))} title="放大" type="button"><Icon name="plus" size={17} /></button>
        <button aria-label="缩小二维图" className="icon-button" onClick={() => setZoom((value) => Math.max(value / 1.25, 0.2))} title="缩小" type="button"><Icon name="minus" size={17} /></button>
        <button className="min-h-10 rounded-md px-2 text-xs font-medium text-slate-600 hover:bg-slate-100" onClick={() => setZoom(1)} type="button">1:1</button>
      </div>
      <div className="flex h-full w-full items-center justify-center overflow-auto p-4">
        {state.error ? (
          <div className="max-w-sm text-center" role="alert">
            <p className="text-sm font-medium text-red-700">二维预览加载失败</p>
            <p className="mt-1 text-xs text-slate-500">请检查文件是否仍然有效，或重新生成模型后再试。</p>
          </div>
        ) : state.content ? (
          <div dangerouslySetInnerHTML={{ __html: state.content }} style={{ transform: `scale(${zoom})`, transformOrigin: "center" }} />
        ) : (
          <div className="flex items-center gap-2 text-sm text-slate-500" role="status"><span className="h-4 w-4 animate-spin rounded-full border-2 border-sky-600 border-t-transparent" />正在加载二维图</div>
        )}
      </div>
    </div>
  );
}

export default function Viewer2D({ svgUrl }: { svgUrl: string | null }) {
  if (!svgUrl) {
    return <div className="flex h-full w-full items-center justify-center bg-white px-6 text-center text-sm text-slate-500">二维图将在生成后显示</div>;
  }
  return <LoadedSvg key={svgUrl} svgUrl={svgUrl} />;
}
