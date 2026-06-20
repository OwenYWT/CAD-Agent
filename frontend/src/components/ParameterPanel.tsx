import { useState, useRef, useCallback, useEffect } from "react";
import type { ParamConfig, ValidationData } from "../types";

interface ParameterPanelProps {
  params: Record<string, ParamConfig> | null;
  code: string | null;
  onCodeChange: (code: string) => void;
  validation?: ValidationData | null;
}

function smartRange(name: string, value: number) {
  if (/angle|deg/i.test(name)) return { min: 0, max: 360, step: 1 };
  if (/count|num|quantity/i.test(name)) return { min: 1, max: Math.max(value * 5, 50), step: 1 };
  if (/wall|thickness|thick/i.test(name)) return { min: 0.5, max: Math.max(value * 4, 20), step: 0.5 };
  if (/diameter|radius|bore|_d$|_r$|_od|_id/i.test(name)) return { min: 1, max: value * 5, step: 0.5 };
  const min = Math.max(value * 0.1, 0.1);
  const max = value * 5;
  const step = value > 10 ? 1 : 0.1;
  return { min, max, step };
}

function ValidationBadge({ validation }: { validation: ValidationData }) {
  const items: { label: string; ok: boolean; detail: string }[] = [];

  if (validation.is_watertight !== undefined) {
    items.push({
      label: "Watertight",
      ok: validation.is_watertight,
      detail: validation.is_watertight ? "Mesh is closed" : "Mesh has gaps",
    });
  }

  if (validation.volume !== undefined) {
    items.push({
      label: "Volume",
      ok: validation.volume > 0,
      detail: `${validation.volume.toFixed(1)} mm\u00B3`,
    });
  }

  if (validation.bounding_box) {
    const bb = validation.bounding_box;
    const dims = `${(bb.x_max - bb.x_min).toFixed(1)} \u00D7 ${(bb.y_max - bb.y_min).toFixed(1)} \u00D7 ${(bb.z_max - bb.z_min).toFixed(1)}`;
    items.push({ label: "Size", ok: true, detail: dims + " mm" });
  }

  if (items.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 mt-2 mb-1">
      {items.map((item) => (
        <span
          key={item.label}
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${
            item.ok ? "bg-green-50 text-green-700" : "bg-red-50 text-red-700"
          }`}
          title={item.detail}
        >
          {item.ok ? "\u2713" : "\u2717"} {item.label}: {item.detail}
        </span>
      ))}
    </div>
  );
}

export default function ParameterPanel({
  params,
  code,
  onCodeChange,
  validation,
}: ParameterPanelProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [localValues, setLocalValues] = useState<Record<string, number>>({});
  const [codeHistory, setCodeHistory] = useState<string[]>([]);
  const [originalCode, setOriginalCode] = useState<string | null>(null);

  // Sync local values when params change (new generation)
  useEffect(() => {
    if (params) {
      const vals: Record<string, number> = {};
      for (const [name, p] of Object.entries(params)) {
        vals[name] = p.value;
      }
      setLocalValues(vals);
    }
  }, [params]);

  // Track original code for reset
  useEffect(() => {
    if (code && !originalCode) {
      setOriginalCode(code);
    }
  }, [code, originalCode]);

  // Save to history before applying change
  const pushHistory = useCallback(() => {
    if (code) {
      setCodeHistory((prev) => [...prev.slice(-20), code]);
    }
  }, [code]);

  const handleUndo = useCallback(() => {
    if (codeHistory.length === 0 || !code) return;
    const prev = codeHistory[codeHistory.length - 1];
    setCodeHistory((h) => h.slice(0, -1));
    onCodeChange(prev);
  }, [codeHistory, code, onCodeChange]);

  const handleChange = useCallback(
    (paramName: string, newValue: number) => {
      if (!code) return;
      setLocalValues((prev) => ({ ...prev, [paramName]: newValue }));

      const regex = new RegExp(
        `(${paramName}\\s*=\\s*)([0-9]+\\.?[0-9]*)`,
        "m",
      );
      const newCode = code.replace(regex, `$1${newValue}`);

      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        pushHistory();
        onCodeChange(newCode);
      }, 500);
    },
    [code, onCodeChange, pushHistory],
  );

  if (!params || Object.keys(params).length === 0) {
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
        <h3 className="text-sm font-medium text-gray-700">参数调整</h3>
        <div className="flex items-center gap-2">
          {codeHistory.length > 0 && (
            <button
              onClick={handleUndo}
              className="text-xs text-indigo-500 hover:text-indigo-700"
              title="撤销上次修改"
            >
              撤销
            </button>
          )}
          {originalCode && code !== originalCode && (
            <button
              onClick={() => {
                if (originalCode) {
                  onCodeChange(originalCode);
                  if (params) {
                    const vals: Record<string, number> = {};
                    for (const [name, p] of Object.entries(params)) {
                      vals[name] = p.value;
                    }
                    setLocalValues(vals);
                  }
                  setCodeHistory([]);
                }
              }}
              className="text-xs text-gray-400 hover:text-red-500"
              title="重置为初始值"
            >
              重置
            </button>
          )}
        </div>
      </div>

      {validation && <ValidationBadge validation={validation} />}

      <div className="space-y-2">
        {Object.entries(params).map(([name, param]) => {
          const { min, max, step } = smartRange(name, param.value);
          const currentValue = localValues[name] ?? param.value;

          return (
            <div key={name} className="flex items-center gap-2 text-xs">
              <label
                className="w-28 truncate text-gray-600"
                title={param.comment}
              >
                {param.comment}
              </label>
              <input
                type="range"
                min={min}
                max={max}
                step={step}
                value={currentValue}
                onChange={(e) =>
                  handleChange(name, parseFloat(e.target.value))
                }
                className="flex-1"
              />
              <input
                type="number"
                min={min}
                max={max}
                step={step}
                value={currentValue}
                onChange={(e) =>
                  handleChange(name, parseFloat(e.target.value))
                }
                className="w-16 border border-gray-300 rounded px-1 py-0.5 text-center"
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
