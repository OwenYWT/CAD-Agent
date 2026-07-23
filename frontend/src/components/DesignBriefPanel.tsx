import type { DesignBrief } from "../types";

interface DesignBriefPanelProps {
  brief?: DesignBrief | null;
}

function Section({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <h4 className="text-xs font-semibold text-gray-700 mb-1">{title}</h4>
      <ul className="space-y-1">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="text-xs text-gray-600 flex gap-2">
            <span className="text-gray-300">-</span>
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function DesignBriefPanel({ brief }: DesignBriefPanelProps) {
  if (!brief) return null;

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-100">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-gray-900">{"\u5de5\u7a0b\u8bbe\u8ba1\u7b80\u62a5"}</h3>
          <span className="text-[10px] uppercase tracking-wide rounded-full border border-blue-200 bg-blue-50 text-blue-700 px-2 py-0.5">
            {brief.manufacturing_posture || "面向 3D 打印"}
          </span>
        </div>
        <p className="text-xs text-gray-500 mt-1">
          {"\u5728\u751f\u6210 CAD \u524d\u5c55\u793a\u8bbe\u8ba1\u5047\u8bbe\u548c\u9a8c\u6536\u53e3\u5f84\uff1b\u8fd9\u4e0d\u662f\u5236\u9020\u8ba4\u8bc1\u3002"}
        </p>
      </div>

      <div className="p-4 space-y-4">
        <div>
          <h4 className="text-xs font-semibold text-gray-700 mb-1">{"\u9700\u6c42\u6458\u8981"}</h4>
          <p className="text-xs text-gray-600">{brief.intent_summary}</p>
          <p className="text-[10px] text-gray-400 mt-1">{"\u5236\u54c1\u7c7b\u578b\uff1a"}{brief.artifact_type}</p>
        </div>

        {brief.critical_dimensions?.length ? (
          <div>
            <h4 className="text-xs font-semibold text-gray-700 mb-1">{"\u5173\u952e\u5c3a\u5bf8"}</h4>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-gray-400">
                  <tr>
                    <th className="text-left font-medium py-1">{"\u540d\u79f0"}</th>
                    <th className="text-left font-medium py-1">{"\u6570\u503c"}</th>
                    <th className="text-left font-medium py-1">{"\u539f\u56e0"}</th>
                  </tr>
                </thead>
                <tbody>
                  {brief.critical_dimensions.map((dimension, index) => (
                    <tr key={`${dimension.name}-${index}`} className="border-t border-gray-100">
                      <td className="py-1 pr-2 text-gray-700">{dimension.name}</td>
                      <td className="py-1 pr-2 text-gray-600 tabular-nums">
                        {dimension.value ?? "?"} {dimension.unit || "mm"}
                      </td>
                      <td className="py-1 text-gray-500">{dimension.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}

        <Section title={"设计假设"} items={brief.assumptions} />
        <Section title={"功能需求"} items={brief.functional_requirements} />
        <Section title={"可打印性目标"} items={brief.printability_targets} />
        <Section title={"验收标准"} items={brief.acceptance_criteria} />
        <Section title={"待确认问题"} items={brief.open_questions} />
      </div>
    </div>
  );
}
