import { useState, useCallback } from "react";

import { authFetch } from "../auth";
const API_BASE = import.meta.env.VITE_API_BASE || "";

interface KGNode {
  id: string;
  type: string;
  name: string;
  properties: Record<string, unknown>;
  customer_id: string;
}

interface ProcessRecommendation {
  process_id: string;
  process_name: string;
  material_id: string | null;
  material_name: string | null;
  constraints: Record<string, unknown>;
  score: number;
  notes: string[];
}

interface MaterialInfo {
  material: KGNode;
  constraints: Record<string, unknown>;
}

const PROCESS_LABELS: Record<string, string> = {
  proc_cnc_3axis: "CNC 3轴",
  proc_cnc_5axis: "CNC 5轴",
  proc_fdm: "FDM",
  proc_sla: "SLA",
  proc_injection: "注塑",
  proc_sheet: "针金",
};

export default function KnowledgeGraph() {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"processes" | "recommend" | "suppliers">("processes");
  const [processes, setProcesses] = useState<KGNode[]>([]);
  const [activeProcess, setActiveProcess] = useState<string>("");
  const [materials, setMaterials] = useState<MaterialInfo[]>([]);
  const [recommendations, setRecommendations] = useState<ProcessRecommendation[]>([]);
  const [recDim, setRecDim] = useState("");
  const [recMat, setRecMat] = useState("");
  const [suppliers, setSuppliers] = useState<KGNode[]>([]);
  const [loading, setLoading] = useState(false);

  const fetchProcesses = useCallback(async () => {
    setLoading(true);
    try {
      const res = await authFetch(`${API_BASE}/api/knowledge/nodes?type=process`);
      if (res.ok) {
        const data: KGNode[] = await res.json();
        setProcesses(data);
        if (!activeProcess && data.length > 0) {
          setActiveProcess(data[0].id);
          const materialsRes = await authFetch(`${API_BASE}/api/knowledge/process/${data[0].id}/materials`);
          if (materialsRes.ok) setMaterials(await materialsRes.json());
        }
      }
    } catch { /* ignore */ } finally {
      setLoading(false);
    }
  }, [activeProcess]);

  const fetchMaterials = useCallback(async (processId: string) => {
    try {
      const res = await authFetch(`${API_BASE}/api/knowledge/process/${processId}/materials`);
      if (res.ok) setMaterials(await res.json());
    } catch { /* ignore */ }
  }, []);

  const fetchSuppliers = useCallback(async () => {
    try {
      const res = await authFetch(`${API_BASE}/api/knowledge/nodes?type=supplier`);
      if (res.ok) setSuppliers(await res.json());
    } catch { /* ignore */ }
  }, []);

  const handleRecommend = async () => {
    setLoading(true);
    try {
      const res = await authFetch(`${API_BASE}/api/knowledge/recommend`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          max_dimension: recDim ? parseFloat(recDim) : null,
          material: recMat || null,
        }),
      });
      if (res.ok) setRecommendations(await res.json());
    } catch { /* ignore */ } finally {
      setLoading(false);
    }
  };

  const handleAddSupplier = async () => {
    const name = prompt("输入供应商名称:");
    if (!name) return;
    const id = `supplier_${name.toLowerCase().replace(/\s+/g, "_")}`;
    await authFetch(`${API_BASE}/api/knowledge/nodes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id,
        type: "supplier",
        name,
        properties: { capabilities: [] },
      }),
    });
    fetchSuppliers();
  };

  const handleDeleteSupplier = async (nodeId: string) => {
    await authFetch(`${API_BASE}/api/knowledge/nodes/${nodeId}?customer_id=default`, {
      method: "DELETE",
    });
    setSuppliers((prev) => prev.filter((s) => s.id !== nodeId));
  };

  if (!open) {
    return (
      <button
        onClick={() => {
          setOpen(true);
          if (processes.length === 0) void fetchProcesses();
        }}
        className="ww-settings-entry"
      >
        工艺知识图谱
      </button>
    );
  }

  return (
    <div className="space-y-3 border-t border-[var(--line)] bg-[var(--surface-soft)] p-4">
      <div className="flex items-center justify-between">
        <h4 className="type-section-heading  text-[var(--ink)]">工艺知识图谱</h4>
        <button
          onClick={() => setOpen(false)}
          className="type-control text-[var(--faint)] hover:text-[var(--ink)]"
        >
          收起
        </button>
      </div>

      {/* Tabs */}
      <div className="flex gap-1">
        {(["processes", "recommend", "suppliers"] as const).map((t) => (
          <button
            key={t}
            onClick={() => {
              setTab(t);
              if (t === "suppliers") void fetchSuppliers();
            }}
            className={`min-h-8 rounded-md border px-2 type-control ${
              tab === t
                ? "border-[var(--agent-border)] bg-[var(--agent-soft)] text-[var(--agent)]"
                : "border-[var(--line)] bg-[var(--surface)] text-[var(--muted)] hover:bg-[var(--subtle)]"
            }`}
          >
            {t === "processes" ? "工艺-材料" : t === "recommend" ? "智能推荐" : "供应商"}
          </button>
        ))}
      </div>

      {loading && <p className="type-body text-[var(--faint)]">加载中...</p>}

      {/* Process-Material tab */}
      {tab === "processes" && (
        <>
          <div className="flex gap-1 flex-wrap">
            {processes.map((p) => (
              <button
                key={p.id}
                onClick={() => {
                  setActiveProcess(p.id);
                  void fetchMaterials(p.id);
                }}
                className={`min-h-8 rounded-md border px-2 type-control ${
                  activeProcess === p.id
                    ? "border-[var(--agent-border)] bg-[var(--agent-soft)] text-[var(--agent)]"
                    : "border-[var(--line)] bg-[var(--surface)] text-[var(--muted)] hover:bg-[var(--subtle)]"
                }`}
              >
                {PROCESS_LABELS[p.id] || p.name}
              </button>
            ))}
          </div>

          <div className="space-y-1 max-h-40 overflow-y-auto">
            {materials.map((m) => (
              <div key={m.material.id} className="flex items-start gap-2 rounded-md border border-[var(--line)] bg-[var(--surface)] p-2 type-body">
                <span className="shrink-0  text-[var(--ink)]">{m.material.name}</span>
                <div className="flex flex-wrap gap-1 text-[var(--faint)]">
                  {m.constraints.min_wall != null && (
                    <span className="rounded bg-[var(--subtle)] px-1">壁厚≥{String(m.constraints.min_wall)}mm</span>
                  )}
                  {m.constraints.max_size != null && (
                    <span className="rounded bg-[var(--subtle)] px-1">尺寸≤{String(m.constraints.max_size)}mm</span>
                  )}
                  {m.constraints.tolerance != null && (
                    <span className="rounded bg-[var(--subtle)] px-1">±{String(m.constraints.tolerance)}mm</span>
                  )}
                  {m.constraints.draft_angle != null && (
                    <span className="rounded bg-[var(--subtle)] px-1">拔模≥{String(m.constraints.draft_angle)}°</span>
                  )}
                </div>
              </div>
            ))}
            {materials.length === 0 && !loading && (
              <p className="type-body text-[var(--faint)]">无支持材料记录</p>
            )}
          </div>
        </>
      )}

      {/* Recommend tab */}
      {tab === "recommend" && (
        <>
          <div className="flex gap-1.5 items-end">
            <div>
              <label className="type-control text-[var(--faint)]">最大尺寸 (mm)</label>
              <input
                type="number"
                value={recDim}
                onChange={(e) => setRecDim(e.target.value)}
                className="min-h-9 w-20 rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-2 type-control"
                placeholder="200"
              />
            </div>
            <div>
              <label className="type-control text-[var(--faint)]">材料偏好</label>
              <input
                type="text"
                value={recMat}
                onChange={(e) => setRecMat(e.target.value)}
                className="min-h-9 w-24 rounded-md border border-[var(--line-strong)] bg-[var(--surface)] px-2 type-control"
                placeholder="铝合金"
              />
            </div>
            <button
              onClick={handleRecommend}
              className="workspace-button workspace-button--primary min-h-9"
            >
              推荐
            </button>
          </div>

          {recommendations.length > 0 && (
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {recommendations.map((r) => (
                <div
                  key={r.process_id}
                  className={`type-body rounded p-1.5 ${
                    r.score >= 0.7
                      ? "bg-emerald-50 border border-emerald-200"
                      : r.score >= 0.3
                        ? "bg-amber-50 border border-amber-200"
                        : "bg-red-50 border border-red-200"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className=" text-[var(--ink)]">{r.process_name}</span>
                    <span className={` ${
                      r.score >= 0.7 ? "text-emerald-600" : r.score >= 0.3 ? "text-amber-600" : "text-red-500"
                    }`}>
                      {(r.score * 100).toFixed(0)}%
                    </span>
                  </div>
                  {r.material_name && (
                    <div className="text-[var(--muted)]">材料: {r.material_name}</div>
                  )}
                  {r.notes.map((n, i) => (
                    <div key={i} className="text-[var(--faint)]">{n}</div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {/* Suppliers tab */}
      {tab === "suppliers" && (
        <>
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {suppliers.map((s) => (
              <div key={s.id} className="flex items-center justify-between rounded-md border border-[var(--line)] bg-[var(--surface)] p-2 type-body">
                <span className=" text-[var(--ink)]">{s.name}</span>
                <button
                  onClick={() => handleDeleteSupplier(s.id)}
                  className="text-red-400 hover:text-red-600"
                >
                  删除
                </button>
              </div>
            ))}
            {suppliers.length === 0 && !loading && (
              <p className="type-body text-[var(--faint)]">暂无供应商记录</p>
            )}
          </div>
          <button
            onClick={handleAddSupplier}
            className="type-control  text-[var(--agent)] hover:underline"
          >
            + 添加供应商
          </button>
        </>
      )}
    </div>
  );
}
