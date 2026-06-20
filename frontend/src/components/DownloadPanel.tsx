interface DownloadPanelProps {
  files: Record<string, string> | null;
  requestId: string | null;
  hasResult: boolean;
}

const FILE_ICONS: Record<string, string> = {
  step: "STEP",
  stl: "STL",
  dxf: "DXF",
  svg: "SVG",
  png: "PNG",
};

const FILE_LABELS: Record<string, string> = {
  step: "STEP 3D \u6a21\u578b",
  stl: "STL \u9884\u89c8/\u6253\u5370",
  dxf: "DXF 2D \u56fe\u7eb8",
  svg: "SVG 2D \u9884\u89c8",
  png: "PNG \u6e32\u67d3\u56fe",
};

const FILE_DESCRIPTIONS: Record<string, string> = {
  step: "\u7528\u4e8e\u673a\u68b0 CAD\u3001\u88c5\u914d\u548c\u540e\u7eed\u7f16\u8f91",
  stl: "\u7528\u4e8e\u7f51\u9875\u9884\u89c8\u3001\u5207\u7247\u548c 3D \u6253\u5370",
  dxf: "\u7528\u4e8e 2D CAD\u3001\u6fc0\u5149\u5207\u5272\u548c\u5de5\u7a0b\u56fe",
  svg: "\u7528\u4e8e\u6d4f\u89c8\u5668\u67e5\u770b\u548c\u6587\u6863\u5c55\u793a",
  png: "\u7528\u4e8e\u5feb\u901f\u5206\u4eab\u548c\u65b9\u6848\u6c47\u62a5",
};

export default function DownloadPanel({ files, requestId, hasResult }: DownloadPanelProps) {
  if (!hasResult) {
    return (
      <div className="p-4 text-sm text-gray-500">
        {"\u751f\u6210\u6a21\u578b\u540e\uff0c\u53ef\u5728\u8fd9\u91cc\u4e0b\u8f7d STEP\u3001STL\u3001DXF\u3001SVG \u7b49\u7f51\u9875\u7aef\u4ea7\u7269\u3002"}
      </div>
    );
  }

  const fileEntries = files ? Object.entries(files).filter(([, url]) => !!url) : [];

  return (
    <div className="p-4 space-y-4">
      <div>
        <h4 className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">
          {"\u6a21\u578b\u6587\u4ef6"}
        </h4>
        {fileEntries.length > 0 ? (
          <div className="space-y-2">
            {fileEntries.map(([format, url]) => (
              <a
                key={format}
                href={url}
                download
                className="flex items-center gap-3 px-3 py-2.5 bg-white border border-gray-200 rounded-lg hover:border-indigo-300 hover:bg-indigo-50 transition-colors group"
              >
                <span className="text-xs font-semibold text-indigo-500 w-10">{FILE_ICONS[format] || "FILE"}</span>
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-gray-700 group-hover:text-indigo-700">
                    {FILE_LABELS[format] || format.toUpperCase()}
                  </div>
                  <div className="text-[11px] text-gray-400 truncate">
                    {FILE_DESCRIPTIONS[format] || "\u70b9\u51fb\u4e0b\u8f7d\u751f\u6210\u6587\u4ef6"}
                  </div>
                </div>
                <span className="text-xs text-indigo-500">{"\u4e0b\u8f7d"}</span>
              </a>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-dashed border-gray-200 bg-white p-3 text-sm text-gray-500">
            {"\u5f53\u524d\u7ed3\u679c\u6ca1\u6709\u53ef\u4e0b\u8f7d\u6587\u4ef6\u3002"}
          </div>
        )}
      </div>

      <div className="rounded-lg bg-indigo-50 border border-indigo-100 p-3 text-xs text-indigo-700 leading-5">
        <div className="font-medium mb-1">Web {"\u7248\u5de5\u4f5c\u6d41"}</div>
        <p>{"\u5728\u6d4f\u89c8\u5668\u5b8c\u6210\u751f\u6210\u3001\u9884\u89c8\u3001\u53c2\u6570\u8c03\u6574\u548c\u4e0b\u8f7d\uff1b\u4e0d\u518d\u4f9d\u8d56\u684c\u9762 CAD \u63d2\u4ef6\u3002"}</p>
        {requestId && <p className="mt-1 text-indigo-500">{"\u8bf7\u6c42 ID\uff1a"}{requestId}</p>}
      </div>
    </div>
  );
}
