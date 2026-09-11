import type { DesignBrief } from "../types";

interface DesignBriefPanelProps {
  brief?: DesignBrief | null;
}

function Section({ title, items }: { title: string; items?: string[] }) {
  if (!items?.length) return null;
  return (
    <div>
      <h4 className="type-section-heading  text-[var(--ink)] mb-1">{title}</h4>
      <ul className="space-y-1">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="type-body text-[var(--muted)] flex gap-2">
            <span className="text-[var(--faint)]">-</span>
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
    <div className="bg-[var(--surface)] rounded-xl border border-[var(--line)] shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-[var(--line)]">
        <div className="flex items-center justify-between gap-3">
          <h3 className="type-section-heading  text-[var(--ink)]">{"\u5de5\u7a0b\u8bbe\u8ba1\u7b80\u62a5"}</h3>
          <span className="type-caption uppercase tracking-wide rounded-full border border-[var(--agent-border)] bg-[var(--agent-soft)] text-[var(--agent)] px-2 py-0.5">
            {brief.manufacturing_posture || "\u9762\u5411 3D \u6253\u5370"}
          </span>
        </div>
        <p className="type-body text-[var(--muted)] mt-1">
          {"\u5728\u751f\u6210 CAD \u524d\u5c55\u793a\u8bbe\u8ba1\u5047\u8bbe\u548c\u9a8c\u6536\u53e3\u5f84\uff1b\u8fd9\u4e0d\u662f\u5236\u9020\u8ba4\u8bc1\u3002"}
        </p>
      </div>

      <div className="p-4 space-y-4">
        <div>
          <h4 className="type-section-heading  text-[var(--ink)] mb-1">{"\u9700\u6c42\u6458\u8981"}</h4>
          <p className="type-body text-[var(--muted)]">{brief.intent_summary}</p>
          <p className="type-caption text-[var(--faint)] mt-1">{"\u5236\u4ef6\u7c7b\u578b\uff1a"}{brief.artifact_type}</p>
        </div>

        {brief.critical_dimensions?.length ? (
          <div>
            <h4 className="type-section-heading  text-[var(--ink)] mb-1">{"\u5173\u952e\u5c3a\u5bf8"}</h4>
            <div className="overflow-x-auto">
              <table className="w-full type-body">
                <thead className="text-[var(--faint)]">
                  <tr>
                    <th className="text-left  py-1">{"\u540d\u79f0"}</th>
                    <th className="text-left  py-1">{"\u6570\u503c"}</th>
                    <th className="text-left  py-1">{"\u539f\u56e0"}</th>
                  </tr>
                </thead>
                <tbody>
                  {brief.critical_dimensions.map((dimension, index) => (
                    <tr key={`${dimension.name}-${index}`} className="border-t border-[var(--line)]">
                      <td className="py-1 pr-2 text-[var(--ink)]">{dimension.name}</td>
                      <td className="py-1 pr-2 text-[var(--muted)] tabular-nums">
                        {dimension.value ?? "?"} {dimension.unit || "mm"}
                      </td>
                      <td className="py-1 text-[var(--muted)]">{dimension.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}

        <Section title={"\u8bbe\u8ba1\u5047\u8bbe"} items={brief.assumptions} />
        <Section title={"\u529f\u80fd\u9700\u6c42"} items={brief.functional_requirements} />
        <Section title={"\u53ef\u6253\u5370\u6027\u76ee\u6807"} items={brief.printability_targets} />
        <Section title={"\u9a8c\u6536\u6807\u51c6"} items={brief.acceptance_criteria} />
        <Section title={"\u5f85\u786e\u8ba4\u95ee\u9898"} items={brief.open_questions} />
      </div>
    </div>
  );
}
