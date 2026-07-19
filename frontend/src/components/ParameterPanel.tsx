import { useState, useRef, useCallback, useEffect, useMemo } from "react";
import type { CADParameter, DesignBrief, ParamConfig, ValidationData } from "../types";
import { splitEngineeringParameters } from "../utils/parameterMapping";

interface ParameterPanelProps {
  params: Record<string, ParamConfig> | null;
  parameters?: CADParameter[] | null;
  code: string | null;
  onCodeChange: (code: string) => void;
  validation?: ValidationData | null;
  designBrief?: DesignBrief | null;
}

function smartRange(name: string, value: number) {
  if (/angle|deg/i.test(name)) return { min: 0, max: 360, step: 1 };
  if (/count|num|quantity/i.test(name)) return { min: 1, max: Math.max(value * 5, 50), step: 1 };
  if (/wall|thickness|thick/i.test(name)) return { min: 0.5, max: Math.max(value * 4, 20), step: 0.5 };
  if (/diameter|radius|bore|_d$|_r$|_od|_id/i.test(name)) return { min: 1, max: Math.max(value * 5, 10), step: 0.5 };
  const min = Math.max(value * 0.1, 0.1);
  const max = Math.max(value * 5, value + 10);
  const step = value > 10 ? 1 : 0.1;
  return { min, max, step };
}

function normalizeLegacyParams(params: Record<string, ParamConfig> | null): CADParameter[] {
  if (!params) return [];
  return Object.entries(params).map(([name, param], index) => {
    const range = smartRange(name, param.value);
    return {
      name,
      display_name: param.comment || name,
      value: param.value,
      default_value: param.value,
      type: "number",
      min: range.min,
      max: range.max,
      step: range.step,
      unit: /_mm$|width|height|depth|radius|diameter|thickness/i.test(name) ? "mm" : null,
      group: "\u53c2\u6570",
      comment: param.comment || name,
      line: index + 1,
    };
  });
}

function rangeFor(parameter: CADParameter) {
  const fallback = smartRange(parameter.name, parameter.value);
  return {
    min: parameter.min ?? fallback.min,
    max: parameter.max ?? fallback.max,
    step: parameter.step ?? fallback.step,
  };
}

function formatNumber(value: number) {
  return Number.isInteger(value) ? String(value) : Number(value.toFixed(6)).toString();
}

function applyParameterValue(code: string, name: string, value: number) {
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(`(^\\s*${escapedName}\\s*=\\s*)-?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(\\s*(?:#.*)?$)`, "m");
  return code.replace(regex, `$1${formatNumber(value)}$2`);
}

function ValidationBadge({ validation }: { validation: ValidationData }) {
  const items: { label: string; ok: boolean; detail: string }[] = [];

  if (validation.is_watertight !== undefined) {
    items.push({
      label: "\u6c34\u5bc6\u6027",
      ok: validation.is_watertight,
      detail: validation.is_watertight ? "\u7f51\u683c\u5df2\u95ed\u5408" : "\u7f51\u683c\u5b58\u5728\u7f1d\u9699",
    });
  }

  if (validation.volume !== undefined) {
    items.push({
      label: "\u4f53\u79ef",
      ok: validation.volume > 0,
      detail: `${validation.volume.toFixed(1)} mm3`,
    });
  }

  if (validation.bounding_box) {
    const bb = validation.bounding_box;
    const dims = `${(bb.x_max - bb.x_min).toFixed(1)} x ${(bb.y_max - bb.y_min).toFixed(1)} x ${(bb.z_max - bb.z_min).toFixed(1)}`;
    items.push({ label: "\u4f53\u79ef", ok: true, detail: dims + " mm" });
  }

  if (items.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 mt-2 mb-3">
      {items.map((item) => (
        <span
          key={item.label}
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${
            item.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"
          }`}
          title={item.detail}
        >
          {item.ok ? "OK" : "!"} {item.label}: {item.detail}
        </span>
      ))}
    </div>
  );
}

export default function ParameterPanel({
  params,
  parameters,
  code,
  onCodeChange,
  validation,
  designBrief,
}: ParameterPanelProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [localValues, setLocalValues] = useState<Record<string, number>>({});
  const [codeHistory, setCodeHistory] = useState<string[]>([]);
  const [originalCode, setOriginalCode] = useState<string | null>(null);

  const editableParameters = useMemo(
    () => (parameters && parameters.length > 0 ? parameters : normalizeLegacyParams(params)),
    [parameters, params],
  );

  useEffect(() => {
    const vals: Record<string, number> = {};
    for (const parameter of editableParameters) {
      vals[parameter.name] = parameter.value;
    }
    setLocalValues(vals);
  }, [editableParameters]);

  useEffect(() => {
    if (code && !originalCode) {
      setOriginalCode(code);
    }
  }, [code, originalCode]);

  const engineeringParameters = useMemo(
    () => splitEngineeringParameters(editableParameters, designBrief ?? null),
    [editableParameters, designBrief],
  );

  const groupedParameters = useMemo(() => {
    const groups = new Map<string, CADParameter[]>();
    for (const parameter of engineeringParameters.standard) {
      const group = parameter.group || "Parameters";
      groups.set(group, [...(groups.get(group) || []), parameter]);
    }
    return Array.from(groups.entries());
  }, [engineeringParameters.standard]);

  const pushHistory = useCallback(() => {
    if (code) {
      setCodeHistory((prev) => [...prev.slice(-20), code]);
    }
  }, [code]);

  const handleUndo = useCallback(() => {
    if (codeHistory.length === 0) return;
    const prev = codeHistory[codeHistory.length - 1];
    setCodeHistory((h) => h.slice(0, -1));
    onCodeChange(prev);
  }, [codeHistory, onCodeChange]);

  const handleChange = useCallback(
    (parameter: CADParameter, newValue: number) => {
      if (!code || Number.isNaN(newValue)) return;
      const { min, max } = rangeFor(parameter);
      const clampedValue = Math.min(Math.max(newValue, min), max);
      setLocalValues((prev) => ({ ...prev, [parameter.name]: clampedValue }));
      const newCode = applyParameterValue(code, parameter.name, clampedValue);

      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        pushHistory();
        onCodeChange(newCode);
      }, 500);
    },
    [code, onCodeChange, pushHistory],
  );

  if (editableParameters.length === 0) {
    if (validation) {
      return (
        <div className="bg-white p-3">
          <ValidationBadge validation={validation} />
        </div>
      );
    }
    return null;
  }

  return (
    <div className="bg-white p-3 overflow-y-auto">
      <div className="flex items-center justify-between mb-1">
        <div>
          <h3 className="text-sm font-medium text-gray-700">{"\u5de5\u7a0b\u53c2\u6570"}</h3>
          <p className="text-[11px] text-gray-400">Edit exposed CAD parameters without asking the LLM.</p>
        </div>
        <div className="flex items-center gap-2">
          {codeHistory.length > 0 && (
            <button
              onClick={handleUndo}
              className="text-xs text-indigo-500 hover:text-indigo-700"
              title="Undo last parameter edit"
            >
              Undo
            </button>
          )}
          {originalCode && code !== originalCode && (
            <button
              onClick={() => {
                onCodeChange(originalCode);
                setCodeHistory([]);
              }}
              className="text-xs text-gray-400 hover:text-red-500"
              title="Reset to original generated code"
            >
              Reset
            </button>
          )}
        </div>
      </div>

      {validation && <ValidationBadge validation={validation} />}

      <div className="space-y-4">
        {engineeringParameters.critical.length > 0 && (
          <section className="space-y-2 rounded-lg border border-indigo-100 bg-indigo-50/40 p-2">
            <div className="flex items-center justify-between text-[11px] font-semibold uppercase tracking-wide text-indigo-700">
              <span>{"\u5173\u952e\u5c3a\u5bf8"}</span>
              <span>{engineeringParameters.critical.length}</span>
            </div>
            {engineeringParameters.critical.map(({ parameter, dimension }) => {
              const { min, max, step } = rangeFor(parameter);
              const currentValue = localValues[parameter.name] ?? parameter.value;
              const label = parameter.comment || parameter.display_name || parameter.name;

              return (
                <div key={parameter.name} className="space-y-1 rounded-md bg-white p-2 text-xs shadow-sm">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <label className="truncate text-gray-700" title={parameter.name}>
                          {label}
                        </label>
                        <span className="rounded-full bg-indigo-100 px-1.5 py-0.5 text-[9px] font-semibold text-indigo-700">
                          Critical
                        </span>
                      </div>
                      {dimension?.reason && (
                        <div className="mt-0.5 text-[10px] leading-snug text-gray-400">{dimension.reason}</div>
                      )}
                    </div>
                    {parameter.unit && <span className="text-[10px] text-gray-400">{parameter.unit}</span>}
                  </div>
                  <div className="flex items-center gap-2">
                    <input
                      type="range"
                      min={min}
                      max={max}
                      step={step}
                      value={currentValue}
                      onChange={(e) => handleChange(parameter, parseFloat(e.target.value))}
                      className="flex-1 accent-indigo-600"
                    />
                    <input
                      type="number"
                      min={min}
                      max={max}
                      step={step}
                      value={currentValue}
                      onChange={(e) => handleChange(parameter, parseFloat(e.target.value))}
                      className="w-20 border border-indigo-200 rounded px-1 py-0.5 text-center"
                    />
                  </div>
                  <div className="flex justify-between text-[10px] text-gray-400">
                    <span>{formatNumber(min)}</span>
                    <span>step {formatNumber(step)}</span>
                    <span>{formatNumber(max)}</span>
                  </div>
                </div>
              );
            })}
          </section>
        )}

        {groupedParameters.map(([group, groupParameters]) => (
          <section key={group} className="space-y-2">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-gray-400 border-b border-gray-100 pb-1">
              {group}
            </div>
            {groupParameters.map((parameter) => {
              const { min, max, step } = rangeFor(parameter);
              const currentValue = localValues[parameter.name] ?? parameter.value;
              const label = parameter.comment || parameter.display_name || parameter.name;

              return (
                <div key={parameter.name} className="space-y-1 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <label className="truncate text-gray-600" title={parameter.name}>
                      {label}
                    </label>
                    {parameter.unit && <span className="text-[10px] text-gray-400">{parameter.unit}</span>}
                  </div>
                  <div className="flex items-center gap-2">
                    <input
                      type="range"
                      min={min}
                      max={max}
                      step={step}
                      value={currentValue}
                      onChange={(e) => handleChange(parameter, parseFloat(e.target.value))}
                      className="flex-1"
                    />
                    <input
                      type="number"
                      min={min}
                      max={max}
                      step={step}
                      value={currentValue}
                      onChange={(e) => handleChange(parameter, parseFloat(e.target.value))}
                      className="w-20 border border-gray-300 rounded px-1 py-0.5 text-center"
                    />
                  </div>
                  <div className="flex justify-between text-[10px] text-gray-400">
                    <span>{formatNumber(min)}</span>
                    <span>step {formatNumber(step)}</span>
                    <span>{formatNumber(max)}</span>
                  </div>
                </div>
              );
            })}
          </section>
        ))}
      </div>
    </div>
  );
}
