import { useEffect, useId } from "react";
import { CAPABILITY_GROUPS } from "../capabilities/manifest";
import { useCapabilities } from "../hooks/useCapabilities";
import type { CapabilitySelection } from "../types";

export type { CapabilitySelection } from "../types";

const CHAT_CAPABILITIES = new Set(["cad", "dxf"]);

export default function CapabilityPicker({ value, onChange }: { value: CapabilitySelection; onChange: (value: CapabilitySelection) => void }) {
  const { capabilities, error, loading } = useCapabilities();
  const selected = capabilities.find((item) => item.id === value);
  const selectId = useId();
  const statusId = useId();

  useEffect(() => {
    if (selected?.available === false) onChange("auto");
  }, [onChange, selected?.available]);

  const chatCapabilities = capabilities.filter((item) => CHAT_CAPABILITIES.has(item.id));
  const status = loading
    ? "正在检查能力"
    : error
      ? "使用内置能力清单，运行状态未知"
      : "运行状态已同步";

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <label className="shrink-0 text-[11px] font-medium text-slate-500" htmlFor={selectId}>工作流</label>
        <select
          aria-describedby={statusId}
          className="min-h-9 min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-2 text-xs text-slate-700 outline-none focus:border-sky-500 focus:ring-2 focus:ring-sky-100"
          id={selectId}
          onChange={(event) => onChange(event.target.value as CapabilitySelection)}
          value={value}
        >
          <option value="auto">自动选择</option>
          {CAPABILITY_GROUPS.map((group) => {
            const items = chatCapabilities.filter((item) => item.group === group);
            if (items.length === 0) return null;
            return (
              <optgroup key={group} label={group}>
                {items.map((item) => (
                <option disabled={item.available === false} key={item.id} value={item.id}>
                  {item.name}{item.available === false ? "（依赖未就绪）" : ""}
                </option>
                ))}
              </optgroup>
            );
          })}
        </select>
        <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${loading ? "bg-amber-400" : error ? "bg-slate-300" : "bg-emerald-500"}`} title={status} />
        <span className="sr-only" id={statusId} role="status">{status}</span>
      </div>
      {selected && (
        <div className={`rounded-md px-2 py-1.5 text-[10px] ${selected.available === false ? "bg-amber-50 text-amber-800" : "bg-slate-50 text-slate-500"}`}>
          <p>{selected.summary}</p>
          {selected.blocked_reasons?.length ? <p className="mt-0.5">当前受限：{selected.blocked_reasons.join("；")}</p> : null}
          {selected.risk_level === "physical_action" ? <p className="mt-0.5 font-medium text-red-700">真实设备动作需要后端确认与安全检查。</p> : null}
        </div>
      )}
    </div>
  );
}
