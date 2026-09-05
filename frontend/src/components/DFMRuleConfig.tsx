import { useState } from "react";
import type { DFMRuleData } from "../types";

import { authFetch } from "../auth";
const API_BASE = import.meta.env.VITE_API_BASE || "";

const PROCESS_LABELS: Record<string, string> = {
  CNC: "CNC 加工",
  FDM: "FDM 打印",
  SLA: "SLA 打印",
  injection_mold: "注塑成型",
  sheet_metal: "针金加工",
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
        onClick={() => {
          setOpen(true);
          if (ruleSets.length === 0) void fetchRuleSets();
        }}
        className="ww-settings-entry"
      >
        DFM 规则配置
      </button>
    );
  }

  const activeRuleSet = ruleSets.find((rs) => rs.process === activeProcess);
  const processes = [...new Set(ruleSets.map((rs) => rs.process))];

  return (
    <div className="space-y-3 border-t border-[var(--line)] bg-[var(--surface-soft)] p-4">
      <div className="flex items-center justify-between">
        <h4 className="type-section-heading  text-[var(--ink)]">DFM 规则配置</h4>
        <button
          onClick={() => setOpen(false)}
          className="type-control text-[var(--faint)] hover:text-[var(--ink)]"
        >
          收起
        </button>
      </div>

      {loading ? (
        <p className="type-body text-[var(--faint)]">加载中...</p>
      ) : (
        <>
          {/* Process tabs */}
          <div className="flex gap-1 flex-wrap">
            {processes.map((p) => (
              <button
                key={p}
                onClick={() => setActiveProcess(p)}
                className={`min-h-8 rounded-md border px-2 type-control ${
                  activeProcess === p
                    ? "border-[var(--agent-border)] bg-[var(--agent-soft)] text-[var(--agent)]"
                    : "border-[var(--line)] bg-[var(--surface)] text-[var(--muted)] hover:bg-[var(--subtle)]"
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
                  className={`flex items-center gap-2 type-body p-1.5 rounded ${
                    rule.enabled ? "border border-[var(--line)] bg-[var(--surface)]" : "bg-[var(--subtle)] opacity-60"
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
                    <div className="truncate  text-[var(--ink)]">
                      {rule.description}
                    </div>
                    <div className="text-[var(--faint)]">
                      {rule.check_type === "geometric" ? "精确计算" : "AI 推理"}
                      {" · "}
                      {rule.severity}
                    </div>
                  </div>
                  {rule.check_type === "geometric" && rule.threshold_min !== null && (
                    <div className="flex items-center gap-1 shrink-0">
                      <span className="text-[var(--faint)]">≥</span>
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
                        className="w-14 rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-1 py-1 text-center type-control text-[var(--ink)]"
                        step={0.1}
                      />
                      <span className="text-[var(--faint)]">{rule.unit}</span>
                    </div>
                  )}
                  {rule.check_type === "geometric" && rule.threshold_max !== null && (
                    <div className="flex items-center gap-1 shrink-0">
                      <span className="text-[var(--faint)]">≤</span>
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
                        className="w-14 rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-1 py-1 text-center type-control text-[var(--ink)]"
                        step={0.1}
                      />
                      <span className="text-[var(--faint)]">{rule.unit}</span>
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
