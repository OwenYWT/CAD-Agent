import { useCallback, useEffect, useRef, useState } from "react";
import type { ParamConfig, ValidationData } from "../types";
import { Icon } from "./ui/Icon";

interface ParameterPanelProps {
  baselineKey: string;
  params: Record<string, ParamConfig> | null;
  code: string | null;
  isGenerating?: boolean;
  onCodeChange: (code: string) => boolean;
  validation?: ValidationData | null;
}

function smartRange(name: string, value: number) {
  if (/angle|deg/i.test(name)) return { min: 0, max: 360, step: 1 };
  if (/count|num|quantity/i.test(name)) return { min: 1, max: Math.max(value * 5, 50), step: 1 };
  if (/wall|thickness|thick/i.test(name)) return { min: 0.5, max: Math.max(value * 4, 20), step: 0.5 };
  if (/diameter|radius|bore|_d$|_r$|_od|_id/i.test(name)) return { min: 1, max: Math.max(value * 5, 5), step: 0.5 };
  return {
    min: Math.max(value * 0.1, 0.1),
    max: Math.max(value * 5, 5),
    step: value > 10 ? 1 : 0.1,
  };
}

function valuesFromParams(params: Record<string, ParamConfig> | null) {
  return Object.fromEntries(Object.entries(params || {}).map(([name, param]) => [name, param.value]));
}

function escapeRegex(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function ValidationSummary({ validation }: { validation: ValidationData }) {
  const size = validation.bounding_box
    ? `${(validation.bounding_box.x_max - validation.bounding_box.x_min).toFixed(1)} × ${(validation.bounding_box.y_max - validation.bounding_box.y_min).toFixed(1)} × ${(validation.bounding_box.z_max - validation.bounding_box.z_min).toFixed(1)} mm`
    : null;
  const items = [
    { label: "网格闭合", value: validation.is_watertight ? "通过" : "存在缺口", ok: validation.is_watertight },
    { label: "模型体积", value: `${validation.volume.toFixed(1)} mm³`, ok: validation.volume > 0 },
    ...(size ? [{ label: "外形尺寸", value: size, ok: true }] : []),
  ];

  return (
    <div className="mb-4 grid gap-2 sm:grid-cols-3">
      {items.map((item) => (
        <div className={`rounded-md border px-3 py-2 ${item.ok ? "border-emerald-200 bg-emerald-50" : "border-red-200 bg-red-50"}`} key={item.label}>
          <div className={`text-[11px] font-medium ${item.ok ? "text-emerald-700" : "text-red-700"}`}>{item.label}</div>
          <div className="mt-0.5 text-xs tabular-nums text-slate-700">{item.value}</div>
        </div>
      ))}
    </div>
  );
}

export default function ParameterPanel({
  baselineKey,
  params,
  code,
  isGenerating = false,
  onCodeChange,
  validation,
}: ParameterPanelProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftCodeRef = useRef(code);
  const [localValues, setLocalValues] = useState<Record<string, number>>(() => valuesFromParams(params));
  const [codeHistory, setCodeHistory] = useState<string[]>([]);
  const [originalCode] = useState<string | null>(code);
  const [queued, setQueued] = useState(false);

  useEffect(() => {
    if (!queued) draftCodeRef.current = code;
  }, [code, queued]);

  useEffect(() => () => {
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  const execute = useCallback((nextCode: string, previousCode: string) => {
    setCodeHistory((history) => [...history.slice(-19), previousCode]);
    setQueued(false);
    return onCodeChange(nextCode);
  }, [onCodeChange]);

  const handleChange = useCallback((paramName: string, newValue: number) => {
    if (!Number.isFinite(newValue) || !draftCodeRef.current) return;
    const previousCode = draftCodeRef.current;
    const regex = new RegExp(`(${escapeRegex(paramName)}\\s*=\\s*)(-?[0-9]+(?:\\.[0-9]+)?)`, "m");
    const nextCode = previousCode.replace(regex, `$1${newValue}`);
    if (nextCode === previousCode) return;

    draftCodeRef.current = nextCode;
    setLocalValues((values) => ({ ...values, [paramName]: newValue }));
    setQueued(true);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => execute(nextCode, previousCode), 500);
  }, [execute]);

  const handleUndo = () => {
    const previousCode = codeHistory.at(-1);
    if (!previousCode) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    draftCodeRef.current = previousCode;
    setCodeHistory((history) => history.slice(0, -1));
    setQueued(false);
    onCodeChange(previousCode);
  };

  const handleReset = () => {
    if (!originalCode) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    draftCodeRef.current = originalCode;
    setLocalValues(valuesFromParams(params));
    setCodeHistory([]);
    setQueued(false);
    onCodeChange(originalCode);
  };

  if (!params || Object.keys(params).length === 0) {
    return validation ? <div className="p-4"><ValidationSummary validation={validation} /></div> : null;
  }

  return (
    <div className="bg-white p-4" data-baseline-key={baselineKey}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-800">参数调整</h3>
          <p className="mt-0.5 text-xs text-slate-500">修改后将自动重新生成模型</p>
        </div>
        <div className="flex items-center gap-1">
          <button aria-label="撤销参数修改" className="icon-button text-slate-500 hover:bg-slate-100 disabled:opacity-30" disabled={codeHistory.length === 0} onClick={handleUndo} title="撤销" type="button">
            <Icon name="undo" size={17} />
          </button>
          <button aria-label="重置所有参数" className="icon-button text-slate-500 hover:bg-red-50 hover:text-red-600 disabled:opacity-30" disabled={!originalCode} onClick={handleReset} title="重置" type="button">
            <Icon name="rotate" size={17} />
          </button>
        </div>
      </div>

      {validation && <ValidationSummary validation={validation} />}

      {(queued || isGenerating) && (
        <div className="mb-3 flex items-center gap-2 rounded-md bg-sky-50 px-3 py-2 text-xs text-sky-800" role="status">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-sky-600 border-t-transparent" />
          {queued ? "参数已更新，正在提交" : "正在重新生成模型"}
        </div>
      )}

      <div className="space-y-4">
        {Object.entries(params).map(([name, param]) => {
          const { min, max, step } = smartRange(name, param.value);
          const currentValue = localValues[name] ?? param.value;
          return (
            <div className="grid grid-cols-[minmax(100px,1fr)_minmax(120px,2fr)_76px] items-center gap-3 text-xs max-sm:grid-cols-[1fr_76px]" key={name}>
              <label className="min-w-0 text-slate-700" htmlFor={`param-${name}`} title={`${param.comment} (${name})`}>
                <span className="block truncate font-medium">{param.comment || name}</span>
                <span className="block truncate text-[10px] text-slate-400">{name}</span>
              </label>
              <input aria-label={`${param.comment || name}滑块`} className="w-full accent-sky-700 max-sm:col-span-2 max-sm:row-start-2" max={max} min={min} onChange={(event) => handleChange(name, Number(event.target.value))} step={step} type="range" value={currentValue} />
              <input className="min-h-10 w-full rounded-md border border-slate-300 px-2 text-right tabular-nums focus:border-sky-600" id={`param-${name}`} inputMode="decimal" max={max} min={min} onChange={(event) => handleChange(name, Number(event.target.value))} step={step} type="number" value={currentValue} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
