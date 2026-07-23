import type { InspectCheck, InspectReport } from "../types";

interface InspectReportPanelProps {
  report?: InspectReport | null;
}

const VERDICT_META: Record<string, { label: string; className: string }> = {
  pass: { label: "\u901a\u8fc7", className: "border-emerald-200 bg-emerald-50 text-emerald-700" },
  warn: { label: "\u6709\u8b66\u544a", className: "border-amber-200 bg-amber-50 text-amber-700" },
  fail: { label: "\u672a\u901a\u8fc7", className: "border-red-200 bg-red-50 text-red-700" },
};

const CHECK_META: Record<string, string> = {
  pass: "bg-emerald-50 text-emerald-700",
  warn: "bg-amber-50 text-amber-700",
  fail: "bg-red-50 text-red-700",
};

const CHECK_STATUS_LABEL: Record<string, string> = {
  pass: "\u901a\u8fc7",
  warn: "\u8b66\u544a",
  fail: "\u5931\u8d25",
};

function formatMaybeNumber(value: number | null | undefined, unit = "") {
  if (value === null || value === undefined) return "\u672a\u8bc4\u4f30";
  return `${Number(value).toFixed(2)}${unit}`;
}

function formatDimensions(report: InspectReport) {
  const box = report.bounding_box;
  if (!box) return "\u672a\u8bc4\u4f30";
  const x = box.x_max - box.x_min;
  const y = box.y_max - box.y_min;
  const z = box.z_max - box.z_min;
  return `${x.toFixed(1)} x ${y.toFixed(1)} x ${z.toFixed(1)} mm`;
}

function CheckRow({ check }: { check: InspectCheck }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg bg-gray-50 px-3 py-2">
      <div>
        <div className="text-xs font-medium text-gray-700">{check.name}</div>
        <div className="mt-0.5 text-xs text-gray-500">{check.message}</div>
      </div>
      <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${CHECK_META[check.status] || "bg-gray-100 text-gray-600"}`}>
        {CHECK_STATUS_LABEL[check.status] || check.status}
      </span>
    </div>
  );
}

export default function InspectReportPanel({ report }: InspectReportPanelProps) {
  if (!report) return null;

  const verdict = VERDICT_META[report.verdict] || VERDICT_META.warn;
  const warnings = report.print_warnings || [];
  const exports = report.available_exports || [];

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">{"\u53ef\u6253\u5370\u6027\u68c0\u67e5"}</h3>
          <p className="text-xs text-gray-500">{"\u57fa\u4e8e\u51e0\u4f55\u6821\u9a8c\u3001\u5bfc\u51fa\u6587\u4ef6\u548c\u4fee\u590d\u5386\u53f2\u751f\u6210"}</p>
        </div>
        <span className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${verdict.className}`}>
          {verdict.label}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="rounded-lg bg-gray-50 p-2">
          <div className="text-gray-500">{"\u5916\u5f62\u5c3a\u5bf8"}</div>
          <div className="mt-1 font-medium text-gray-900">{formatDimensions(report)}</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-2">
          <div className="text-gray-500">{"\u4f53\u79ef"}</div>
          <div className="mt-1 font-medium text-gray-900">{formatMaybeNumber(report.volume, " mm3")}</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-2">
          <div className="text-gray-500">{"\u6c34\u5bc6\u6027"}</div>
          <div className="mt-1 font-medium text-gray-900">{report.is_watertight ? "\u901a\u8fc7" : "\u5931\u8d25\u6216\u672a\u8bc4\u4f30"}</div>
        </div>
        <div className="rounded-lg bg-gray-50 p-2">
          <div className="text-gray-500">{"\u6700\u5c0f\u58c1\u539a"}</div>
          <div className="mt-1 font-medium text-gray-900">{formatMaybeNumber(report.min_wall_thickness, " mm")}</div>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2 text-xs text-gray-600">
        <span className="rounded-full bg-indigo-50 px-2 py-1 text-indigo-700">{"\u4fee\u590d\u6b21\u6570\uff1a"}{report.repair_attempts || 0}</span>
        <span className="rounded-full bg-slate-100 px-2 py-1">{"\u6765\u6e90\uff1a"}{report.source || "geometry_validator"}</span>
        {exports.length > 0 && (
          <span className="rounded-full bg-blue-50 px-2 py-1 text-blue-700">{"\u5bfc\u51fa\u6587\u4ef6\uff1a"}{exports.join(", ")}</span>
        )}
      </div>

      {warnings.length > 0 && (
        <div className="mt-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
          <div className="mb-1 font-semibold">{"\u6253\u5370\u98ce\u9669\u63d0\u793a"}</div>
          <ul className="list-disc space-y-1 pl-4">
            {warnings.map((warning, index) => (
              <li key={`${warning}-${index}`}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {report.checks.length > 0 && (
        <div className="mt-3 space-y-2">
          {report.checks.map((check, index) => (
            <CheckRow key={`${check.name}-${index}`} check={check} />
          ))}
        </div>
      )}
    </div>
  );
}
