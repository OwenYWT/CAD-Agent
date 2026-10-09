import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { authFetch } from "../auth";
import { Icon } from "./ui/Icon";

const API_BASE = import.meta.env.VITE_API_BASE || "";

function LoadedSvg({ svgUrl, fitRequest }: { svgUrl: string; fitRequest: number }) {
  const [state, setState] = useState<{ objectUrl: string | null; error: boolean }>({ objectUrl: null, error: false });
  const [zoom, setZoom] = useState(1);
  const container = useRef<HTMLDivElement>(null);
  const image = useRef<HTMLImageElement>(null);
  const [natural, setNatural] = useState({ width: 0, height: 0 });
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const fitMode = useRef(true);
  const fittedRequest = useRef(-1);
  useLayoutEffect(() => {
    if (!container.current) return;
    const observer = new ResizeObserver(([entry]) => setViewport({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    if (!natural.width || !natural.height || !viewport.width || !viewport.height) return;
    if (fittedRequest.current !== fitRequest) fitMode.current = true;
    if (!fitMode.current) return;
    setZoom(Math.min(viewport.width / natural.width, viewport.height / natural.height));
    fittedRequest.current = fitRequest;
    container.current?.scrollTo(0, 0);
  }, [fitRequest, natural, viewport]);
  const zoomTo = (value: number) => { fitMode.current = false; setZoom(value); };

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    const target = /^https?:\/\//.test(svgUrl) ? svgUrl : `${API_BASE}${svgUrl}`;
    authFetch(target, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
      })
      .then((text) => {
        objectUrl = URL.createObjectURL(new Blob([text], { type: "image/svg+xml" }));
        setState({ objectUrl, error: false });
      })
      .catch((error) => {
        if (error instanceof Error && error.name !== "AbortError") setState({ objectUrl: null, error: true });
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [svgUrl]);

  return (
    <div className="relative h-full w-full overflow-hidden bg-white">
      <div className="absolute right-3 top-3 z-10 flex gap-1 rounded-md border border-[var(--line)] bg-white p-1 shadow-sm">
        <button aria-label="放大二维图" className="icon-button" onClick={() => zoomTo(Math.min(zoom * 1.25, 5))} title="放大" type="button"><Icon name="plus" size={17} /></button>
        <button aria-label="缩小二维图" className="icon-button" onClick={() => zoomTo(Math.max(zoom / 1.25, 0.05))} title="缩小" type="button"><Icon name="minus" size={17} /></button>
        <button className="workspace-button" onClick={() => zoomTo(1)} type="button">1:1</button>
      </div>
      <div ref={container} data-testid="svg-viewport" data-zoom={zoom} className="h-full w-full overflow-auto p-4">
        {state.error ? (
          <div className="max-w-sm text-center" role="alert">
            <p className="type-body  text-red-700">二维预览加载失败</p>
            <p className="mt-1 type-body text-[var(--muted)]">请检查文件是否仍然有效，或重新生成模型后再试。</p>
          </div>
        ) : state.objectUrl ? (
          <img alt="二维工程图预览" ref={image} src={state.objectUrl} onLoad={() => { if (image.current) setNatural({ width: image.current.naturalWidth, height: image.current.naturalHeight }); }} style={{ width: natural.width ? natural.width * zoom : undefined, height: natural.height ? natural.height * zoom : undefined, maxWidth: "none", margin: "auto" }} />
        ) : (
          <div className="flex items-center gap-2 type-body text-[var(--muted)]" role="status"><span className="h-4 w-4 animate-spin rounded-full border-2 border-[var(--agent)] border-t-transparent" />正在加载二维图</div>
        )}
      </div>
    </div>
  );
}

export default function Viewer2D({ svgUrl, fitRequest = 0 }: { svgUrl: string | null; fitRequest?: number }) {
  if (!svgUrl) {
    return <div className="flex h-full w-full items-center justify-center bg-white px-6 text-center type-body text-[var(--muted)]">二维图将在生成后显示</div>;
  }
  return <LoadedSvg key={svgUrl} svgUrl={svgUrl} fitRequest={fitRequest} />;
}
