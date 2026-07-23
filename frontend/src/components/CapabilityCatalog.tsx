import { CAPABILITY_GROUPS } from "../capabilities/manifest";
import { useCapabilities } from "../hooks/useCapabilities";
import type { CapabilityDefinition, CapabilityRiskLevel } from "../types";

const RISK_LABELS: Record<CapabilityRiskLevel, string> = {
  read_only: "只读",
  compute: "本地计算",
  external_write: "外部写入",
  physical_action: "真实设备动作",
};

const RISK_STYLES: Record<CapabilityRiskLevel, string> = {
  read_only: "bg-slate-100 text-slate-700",
  compute: "bg-sky-50 text-sky-700",
  external_write: "bg-amber-50 text-amber-800",
  physical_action: "bg-red-50 text-red-700",
};

const MATURITY_LABELS: Record<CapabilityDefinition["maturity"], string> = {
  stable: "稳定",
  beta: "Beta",
  experimental: "实验性",
};

function AvailabilityBadge({ capability }: { capability: CapabilityDefinition }) {
  if (capability.available === true) {
    return <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700">依赖就绪</span>;
  }
  if (capability.available === false) {
    return <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-800">依赖受限</span>;
  }
  return <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600">状态未知</span>;
}

function ValueList({ label, values }: { label: string; values: string[] }) {
  return (
    <div>
      <dt className="mb-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">{label}</dt>
      <dd className="flex flex-wrap gap-1">
        {values.length > 0 ? values.map((value) => (
          <span className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-600" key={value}>
            {value}
          </span>
        )) : <span className="text-[10px] text-slate-400">无</span>}
      </dd>
    </div>
  );
}

function CapabilityCard({ capability }: { capability: CapabilityDefinition }) {
  return (
    <article className="rounded-lg border border-slate-200 bg-white">
      <details>
        <summary className="cursor-pointer list-none px-3 py-3 [&::-webkit-details-marker]:hidden">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-1.5">
                <h4 className="text-sm font-semibold text-slate-800">{capability.name}</h4>
                <span className="text-[10px] text-slate-400">{MATURITY_LABELS[capability.maturity]}</span>
              </div>
              <p className="mt-1 text-[11px] leading-5 text-slate-500">{capability.summary}</p>
            </div>
            <AvailabilityBadge capability={capability} />
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${RISK_STYLES[capability.risk_level]}`}>
              风险：{RISK_LABELS[capability.risk_level]}
            </span>
            <span className="text-[10px] text-slate-400">{capability.actions.length} 个 action · 展开详情</span>
          </div>
        </summary>

        <div className="space-y-3 border-t border-slate-100 px-3 py-3">
          {capability.blocked_reasons?.length ? (
            <div className="rounded-md bg-amber-50 px-2.5 py-2 text-[11px] text-amber-900">
              <p className="font-medium">当前阻塞原因</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {capability.blocked_reasons.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            </div>
          ) : null}

          <div>
            <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">Actions（仅说明）</p>
            <ul className="space-y-1">
              {capability.actions.map((action) => (
                <li className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-600" key={action.id}>
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-700">{action.id}</code>
                  <span>{action.name}</span>
                  {action.mode ? <span className="text-[10px] text-slate-400">{RISK_LABELS[action.mode]}</span> : null}
                  {action.available === false ? <span className="text-[10px] font-medium text-amber-700" title={action.blocked_reason || undefined}>当前不可用</span> : null}
                  {action.requires_confirmation ? <span className="text-[10px] font-medium text-red-700">需显式确认</span> : null}
                </li>
              ))}
            </ul>
          </div>

          <dl className="grid gap-3 sm:grid-cols-2">
            <ValueList label="输入" values={capability.accepts} />
            <ValueList label="输出" values={capability.produces} />
          </dl>

          {capability.dependencies.length > 0 ? (
            <div>
              <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">依赖</p>
              <ul className="space-y-1 text-[10px] text-slate-500">
                {capability.dependencies.map((dependency) => (
                  <li className="flex items-start gap-1.5" key={dependency.id}>
                    <span aria-hidden="true" className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${dependency.available === false ? "bg-amber-500" : dependency.available === true ? "bg-emerald-500" : "bg-slate-300"}`} />
                    <span>
                      {dependency.label}{dependency.required ? "（必需）" : "（可选）"}
                      {dependency.detail ? <span className="block text-slate-400">{dependency.detail}</span> : null}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </details>
    </article>
  );
}

export default function CapabilityCatalog() {
  const { capabilities, error, loading, source } = useCapabilities();

  return (
    <section aria-labelledby="capability-catalog-title" className="space-y-4 p-4">
      <div>
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-slate-800" id="capability-catalog-title">能力目录</h3>
          <span className="text-[11px] text-slate-400">{capabilities.length} 项</span>
        </div>
        <p className="mt-1 text-xs leading-5 text-slate-500">
          此目录只展示能力、依赖与风险，不会运行 action。CAD / DXF 可从对话入口选择；其他能力通过结构化 action 和所需产物调用。
        </p>
        <p aria-live="polite" className={`mt-2 rounded-md px-2.5 py-2 text-[11px] ${error ? "bg-slate-100 text-slate-600" : "bg-sky-50 text-sky-700"}`}>
          {loading
            ? "正在同步运行依赖状态…"
            : error
              ? "运行状态同步失败；当前展示内置清单，availability 未知。"
              : source === "server"
                ? "运行依赖状态已从服务端同步。"
                : "当前展示内置能力清单。"}
        </p>
      </div>

      {CAPABILITY_GROUPS.map((group) => {
        const items = capabilities.filter((capability) => capability.group === group);
        if (items.length === 0) return null;
        return (
          <section aria-labelledby={`capability-group-${group}`} className="space-y-2" key={group}>
            <h3 className="text-xs font-semibold text-slate-600" id={`capability-group-${group}`}>{group}</h3>
            {items.map((capability) => <CapabilityCard capability={capability} key={capability.id} />)}
          </section>
        );
      })}
    </section>
  );
}
