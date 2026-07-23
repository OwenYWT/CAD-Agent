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
        className="text-xs text-gray-400 hover:text-gray-600 mt-1"
      >
        工艺知识图谱
      </button>
    );
  }

  return (
    <div className="bg-gray-50 border-t border-gray-200 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-medium text-gray-700">工艺知识图谱</h4>
        <button
          onClick={() => setOpen(false)}
          className="text-xs text-gray-400 hover:text-gray-600"
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
            className={`text-xs px-2 py-0.5 rounded ${
              tab === t
                ? "bg-indigo-500 text-white"
                : "bg-gray-200 text-gray-600 hover:bg-gray-300"
            }`}
          >
            {t === "processes" ? "工艺-材料" : t === "recommend" ? "智能推荐" : "供应商"}
          </button>
        ))}
      </div>

      {loading && <p className="text-xs text-gray-400">加载中...</p>}

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
                className={`text-xs px-2 py-0.5 rounded ${
                  activeProcess === p.id
                    ? "bg-indigo-500 text-white"
                    : "bg-gray-200 text-gray-600 hover:bg-gray-300"
                }`}
              >
                {PROCESS_LABELS[p.id] || p.name}
              </button>
            ))}
          </div>

          <div className="space-y-1 max-h-40 overflow-y-auto">
            {materials.map((m) => (
              <div key={m.material.id} className="text-xs bg-white rounded p-1.5 flex items-start gap-2">
                <span className="font-medium text-gray-700 shrink-0">{m.material.name}</span>
                <div className="text-gray-400 flex flex-wrap gap-1">
                  {m.constraints.min_wall != null && (
                    <span className="bg-gray-100 px-1 rounded">壁厚≥{String(m.constraints.min_wall)}mm</span>
                  )}
                  {m.constraints.max_size != null && (
                    <span className="bg-gray-100 px-1 rounded">尺寸≤{String(m.constraints.max_size)}mm</span>
                  )}
                  {m.constraints.tolerance != null && (
                    <span className="bg-gray-100 px-1 rounded">±{String(m.constraints.tolerance)}mm</span>
                  )}
                  {m.constraints.draft_angle != null && (
                    <span className="bg-gray-100 px-1 rounded">拔模≥{String(m.constraints.draft_angle)}°</span>
                  )}
                </div>
              </div>
            ))}
            {materials.length === 0 && !loading && (
              <p className="text-xs text-gray-400">无支持材料记录</p>
            )}
          </div>
        </>
      )}

      {/* Recommend tab */}
      {tab === "recommend" && (
        <>
          <div className="flex gap-1.5 items-end">
            <div>
              <label className="text-[10px] text-gray-400">最大尺寸 (mm)</label>
              <input
                type="number"
                value={recDim}
                onChange={(e) => setRecDim(e.target.value)}
                className="w-20 text-xs border border-gray-300 rounded px-1.5 py-0.5"
                placeholder="200"
              />
            </div>
            <div>
              <label className="text-[10px] text-gray-400">材料偏好</label>
              <input
                type="text"
                value={recMat}
                onChange={(e) => setRecMat(e.target.value)}
                className="w-24 text-xs border border-gray-300 rounded px-1.5 py-0.5"
                placeholder="铝合金"
              />
            </div>
            <button
              onClick={handleRecommend}
              className="text-xs bg-indigo-500 text-white px-2 py-0.5 rounded hover:bg-indigo-600"
            >
              推荐
            </button>
          </div>

          {recommendations.length > 0 && (
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {recommendations.map((r) => (
                <div
                  key={r.process_id}
                  className={`text-xs rounded p-1.5 ${
                    r.score >= 0.7
                      ? "bg-green-50 border border-green-200"
                      : r.score >= 0.3
                        ? "bg-amber-50 border border-amber-200"
                        : "bg-red-50 border border-red-200"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium text-gray-700">{r.process_name}</span>
                    <span className={`font-bold ${
                      r.score >= 0.7 ? "text-green-600" : r.score >= 0.3 ? "text-amber-600" : "text-red-500"
                    }`}>
                      {(r.score * 100).toFixed(0)}%
                    </span>
                  </div>
                  {r.material_name && (
                    <div className="text-gray-500">材料: {r.material_name}</div>
                  )}
                  {r.notes.map((n, i) => (
                    <div key={i} className="text-gray-400">{n}</div>
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
              <div key={s.id} className="text-xs bg-white rounded p-1.5 flex items-center justify-between">
                <span className="font-medium text-gray-700">{s.name}</span>
                <button
                  onClick={() => handleDeleteSupplier(s.id)}
                  className="text-red-400 hover:text-red-600"
                >
                  删除
                </button>
              </div>
            ))}
            {suppliers.length === 0 && !loading && (
              <p className="text-xs text-gray-400">暂无供应商记录</p>
            )}
          </div>
          <button
            onClick={handleAddSupplier}
            className="text-xs text-indigo-500 hover:text-indigo-700"
          >
            + 添加供应商
          </button>
        </>
      )}
    </div>
  );
}
