import { useState, useEffect } from "react";

function sanitizeSvg(raw: string): string {
  // Remove script tags, event handlers, and potentially dangerous elements
  let cleaned = raw.replace(/<script[\s\S]*?<\/script>/gi, "");
  cleaned = cleaned.replace(/on\w+\s*=\s*"[^"]*"/gi, "");
  cleaned = cleaned.replace(/on\w+\s*=\s*'[^']*'/gi, "");
  cleaned = cleaned.replace(/<iframe[\s\S]*?(<\/iframe>|\/?>)/gi, "");
  cleaned = cleaned.replace(/<object[\s\S]*?(<\/object>|\/?>)/gi, "");
  cleaned = cleaned.replace(/<embed[\s\S]*?\/?>/gi, "");
  cleaned = cleaned.replace(/javascript\s*:/gi, "");
  return cleaned;
}

export default function Viewer2D({ svgUrl }: { svgUrl: string | null }) {
  const [svgContent, setSvgContent] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    if (!svgUrl) {
      setSvgContent(null);
      return;
    }
    fetch(svgUrl)
      .then((r) => r.text())
      .then((text) => setSvgContent(sanitizeSvg(text)))
      .catch(() => setSvgContent(null));
  }, [svgUrl]);

  if (!svgUrl) {
    return (
      <div className="w-full h-full bg-white rounded-lg overflow-hidden relative flex items-center justify-center">
        <p className="text-gray-500 text-lg">
          描述你想要的 2D 图形，AI 将为你生成 DXF 文件
        </p>
      </div>
    );
  }

  return (
    <div className="w-full h-full bg-white rounded-lg overflow-hidden relative">
      <div className="absolute top-3 right-3 z-10 flex gap-1">
        <button
          onClick={() => setZoom((z) => Math.min(z * 1.25, 5))}
          className="w-8 h-8 bg-gray-100 hover:bg-gray-200 rounded text-lg font-bold"
        >
          +
        </button>
        <button
          onClick={() => setZoom((z) => Math.max(z / 1.25, 0.2))}
          className="w-8 h-8 bg-gray-100 hover:bg-gray-200 rounded text-lg font-bold"
        >
          -
        </button>
        <button
          onClick={() => setZoom(1)}
          className="px-2 h-8 bg-gray-100 hover:bg-gray-200 rounded text-sm"
        >
          1:1
        </button>
      </div>

      <div className="w-full h-full overflow-auto flex items-center justify-center p-4">
        {svgContent ? (
          <div
            style={{ transform: `scale(${zoom})`, transformOrigin: "center" }}
            dangerouslySetInnerHTML={{ __html: svgContent }}
          />
        ) : (
          <p className="text-gray-400">Loading SVG...</p>
        )}
      </div>
    </div>
  );
}
