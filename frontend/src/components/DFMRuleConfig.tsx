import { useState, useEffect } from "react";
import type { DFMRuleData } from "../types";

import { authFetch } from "../auth";
const API_BASE = import.meta.env.VITE_API_BASE || "";

const PROCESS_LABELS: Record<string, string> = {
  CNC: "CNC \u52A0\u5DE5",
  FDM: "FDM \u6253\u5370",
  SLA: "SLA \u6253\u5370",
  injection_mold: "\u6CE8\u5851\u6210\u578B",
  sheet_metal: "\u9488\u91D1\u52A0\u5DE5",
};

interface RuleSet {
  id: string;
  name: string;
  process: string;
  rules: DFMRuleData[];
}

export default function DFMRuleConfig() {
  const [open, setOpen] = useState(false);
  const [ruleSets, setRuleSets] = useState<RuleSet[]>([]);
  const [activeProcess, setActiveProcess] = useState<string>("");
  const [loading, setLoading] = useState(false);

  const fetchRuleSets = async () => {
    setLoading(true);
    try {
      const res = await authFetch(`${API_BASE}/api/dfm/rules`);
      if (res.ok) {
        const data: RuleSet[] = await res.json();
        setRuleSets(data);
        if (!activeProcess && data.length > 0) {
          setActiveProcess(data[0].process);
        }
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open && ruleSets.length === 0) {
      fetchRuleSets();
    }
  }, [open]);

  const handleToggleRule = async (ruleId: string, enabled: boolean) => {
    const res = await authFetch(`${API_BASE}/api/dfm/rules/${ruleId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (res.ok) {
      setRuleSets((prev) =>
        prev.map((rs) => ({
          ...rs,
          rules: rs.rules.map((r) =>
            r.id === ruleId ? { ...r, enabled } : r,
          ),
        })),
      );
    }
  };

  const handleUpdateThreshold = async (
    ruleId: string,
    field: "threshold_min" | "threshold_max",
    value: number,
  ) => {
    const res = await authFetch(`${API_BASE}/api/dfm/rules/${ruleId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    if (res.ok) {
      setRuleSets((prev) =>
        prev.map((rs) => ({
          ...rs,
          rules: rs.rules.map((r) =>
            r.id === ruleId ? { ...r, [field]: value } : r,
          ),
        })),
      );
    }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="text-xs text-gray-400 hover:text-gray-600 mt-1"
      >
        DFM \u89C4\u5219\u914D\u7F6E
      </button>
    );
  }

  const activeRuleSet = ruleSets.find((rs) => rs.process === activeProcess);
  const processes = [...new Set(ruleSets.map((rs) => rs.process))];

  return (
    <div className="bg-gray-50 border-t border-gray-200 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-medium text-gray-700">DFM \u89C4\u5219\u914D\u7F6E</h4>
        <button
          onClick={() => setOpen(false)}
          className="text-xs text-gray-400 hover:text-gray-600"
        >
          \u6536\u8D77
        </button>
      </div>

      {loading ? (
        <p className="text-xs text-gray-400">\u52A0\u8F7D\u4E2D...</p>
      ) : (
        <>
          {/* Process tabs */}
          <div className="flex gap-1 flex-wrap">
            {processes.map((p) => (
              <button
                key={p}
                onClick={() => setActiveProcess(p)}
                className={`text-xs px-2 py-0.5 rounded ${
                  activeProcess === p
                    ? "bg-indigo-500 text-white"
                    : "bg-gray-200 text-gray-600 hover:bg-gray-300"
                }`}
              >
                {PROCESS_LABELS[p] || p}
              </button>
            ))}
          </div>

          {/* Rules list */}
          {activeRuleSet && (
            <div className="space-y-1.5 max-h-48 overflow-y-auto">
              {activeRuleSet.rules.map((rule) => (
                <div
                  key={rule.id}
                  className={`flex items-center gap-2 text-xs p-1.5 rounded ${
                    rule.enabled ? "bg-white" : "bg-gray-100 opacity-60"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={rule.enabled}
                    onChange={(e) =>
                      handleToggleRule(rule.id, e.target.checked)
                    }
                    className="shrink-0"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-gray-700 truncate">
                      {rule.description}
                    </div>
                    <div className="text-gray-400">
                      {rule.check_type === "geometric" ? "\u7CBE\u786E\u8BA1\u7B97" : "AI \u63A8\u7406"}
                      {" \u00B7 "}
                      {rule.severity}
                    </div>
                  </div>
                  {rule.check_type === "geometric" && rule.threshold_min !== null && (
                    <div className="flex items-center gap-1 shrink-0">
                      <span className="text-gray-400">\u2265</span>
                      <input
                        type="number"
                        value={rule.threshold_min}
                        onChange={(e) =>
                          handleUpdateThreshold(
                            rule.id,
                            "threshold_min",
                            parseFloat(e.target.value),
                          )
                        }
                        className="w-14 border border-gray-300 rounded px-1 py-0.5 text-center text-xs"
                        step={0.1}
                      />
                      <span className="text-gray-400">{rule.unit}</span>
                    </div>
                  )}
                  {rule.check_type === "geometric" && rule.threshold_max !== null && (
                    <div className="flex items-center gap-1 shrink-0">
                      <span className="text-gray-400">\u2264</span>
                      <input
                        type="number"
                        value={rule.threshold_max}
                        onChange={(e) =>
                          handleUpdateThreshold(
                            rule.id,
                            "threshold_max",
                            parseFloat(e.target.value),
                          )
                        }
                        className="w-14 border border-gray-300 rounded px-1 py-0.5 text-center text-xs"
                        step={0.1}
                      />
                      <span className="text-gray-400">{rule.unit}</span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
