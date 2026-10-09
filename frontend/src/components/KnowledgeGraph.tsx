import { useCallback, useEffect, useRef, useState } from "react";
import { authFetch } from "../auth";
import { API_BASE, readJson } from "../services/clients/http";
import { WorkspaceDialog } from "./common/WorkspaceOverlay";
import { useMutationReceipt } from '../hooks/useMutationReceipt';

interface KGNode { id: string; type: string; name: string; properties: Record<string, unknown>; customer_id: string }
interface ProcessRecommendation { process_id: string; process_name: string; material_id: string | null; material_name: string | null; constraints: Record<string, unknown>; score: number; notes: string[] }
interface MaterialInfo { material: KGNode; constraints: Record<string, unknown> }
type Resource = "processes" | "materials" | "suppliers" | "recommend";
type LoadState = { pending: boolean; error: string; loaded: boolean };
const empty: LoadState = { pending: false, error: "", loaded: false };
const PROCESS_LABELS: Record<string, string> = { proc_cnc_3axis: "CNC 3轴", proc_cnc_5axis: "CNC 5轴", proc_fdm: "FDM", proc_sla: "SLA", proc_injection: "注塑", proc_sheet: "钣金" };

type RecommendationDraft = { dimension: string; material: string };
export default function KnowledgeGraph({ embedded = false, recommendationDraft, onRecommendationDraft }: {
  embedded?: boolean; recommendationDraft?: RecommendationDraft; onRecommendationDraft?: (draft: RecommendationDraft) => void;
}) {
  const [open, setOpen] = useState(embedded);
  const [tab, setTab] = useState<"processes" | "recommend" | "suppliers">("processes");
  const [processes, setProcesses] = useState<KGNode[]>([]);
  const [activeProcess, setActiveProcess] = useState("");
  const [materials, setMaterials] = useState<MaterialInfo[]>([]);
  const [recommendations, setRecommendations] = useState<ProcessRecommendation[]>([]);
  const [localDraft, setLocalDraft] = useState<RecommendationDraft>({ dimension: "", material: "" });
  const draft = recommendationDraft || localDraft;
  const recDim = draft.dimension, recMat = draft.material;
  const setRecDim = (dimension: string) => { const next = { ...draft, dimension }; setLocalDraft(next); onRecommendationDraft?.(next); };
  const setRecMat = (material: string) => { const next = { ...draft, material }; setLocalDraft(next); onRecommendationDraft?.(next); };
  const [suppliers, setSuppliers] = useState<KGNode[]>([]);
  const [states, setStates] = useState<Record<Resource, LoadState>>({ processes: empty, materials: empty, suppliers: empty, recommend: empty });
  const requests = useRef<Partial<Record<Resource, AbortController>>>({});
  const [supplierDialog, setSupplierDialog] = useState(false);
  const [supplierName, setSupplierName] = useState("");
  const [mutation, setMutation] = useState("");
  const [mutationError, setMutationError] = useState("");
  const mutationLock = useRef(false);
  const [uncertainMutation, setUncertainMutation] = useMutationReceipt<{ action: 'create' | 'delete'; target: KGNode }>('supplier');
  const load = useCallback(async <T,>(resource: Resource, path: string, apply: (data: T) => void, options?: RequestInit) => {
    requests.current[resource]?.abort();
    const controller = new AbortController(); requests.current[resource] = controller;
    setStates(previous => ({ ...previous, [resource]: { ...previous[resource], pending: true, error: "" } }));
    try {
      const data = await readJson<T>(await authFetch(API_BASE + path, { ...options, signal: controller.signal }), "请求失败，请重试");
      if (controller.signal.aborted) return;
      if (!Array.isArray(data)) throw new Error("返回的数据格式无效");
      apply(data);
      setStates(previous => ({ ...previous, [resource]: { pending: false, error: "", loaded: true } }));
      return data;
    } catch (reason) {
      if (!controller.signal.aborted) setStates(previous => ({ ...previous, [resource]: { ...previous[resource], pending: false, error: reason instanceof Error ? reason.message : "请求失败" } }));
    }
  }, []);
  useEffect(() => { const current = requests.current; return () => Object.values(current).forEach(controller => controller?.abort()); }, []);
  const fetchProcesses = useCallback(() => load<KGNode[]>("processes", "/api/knowledge/nodes?type=process", data => {
    setProcesses(data); setActiveProcess(previous => data.some(item => item.id === previous) ? previous : data[0]?.id || "");
  }), [load]);
  const fetchMaterials = useCallback((id: string) => load<MaterialInfo[]>("materials", `/api/knowledge/process/${encodeURIComponent(id)}/materials`, setMaterials), [load]);
  const fetchSuppliers = useCallback(() => load<KGNode[]>("suppliers", "/api/knowledge/nodes?type=supplier", setSuppliers), [load]);
  useEffect(() => { if (open) void fetchProcesses(); }, [open, fetchProcesses]);
  useEffect(() => { if (open && tab === "processes" && activeProcess) { setMaterials([]); void fetchMaterials(activeProcess); } }, [open, tab, activeProcess, fetchMaterials]);
  useEffect(() => { if (open && tab === "suppliers") void fetchSuppliers(); }, [open, tab, fetchSuppliers]);
  const handleRecommend = () => {
    const dimension = recDim.trim() ? Number(recDim) : null;
    if (dimension !== null && (!Number.isFinite(dimension) || dimension <= 0)) {
      setStates(previous => ({ ...previous, recommend: { ...previous.recommend, error: "最大尺寸必须为有效正数" } })); return;
    }
    void load<ProcessRecommendation[]>("recommend", "/api/knowledge/recommend", setRecommendations, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ max_dimension: dimension, material: recMat.trim() || null }) });
  };
  const handleAddSupplier = async () => {
    const name = supplierName.trim();
    if (!name || mutationLock.current || uncertainMutation) return;
    mutationLock.current = true; setMutation("create"); setMutationError("");
    const target: KGNode = { id: `supplier_${crypto.randomUUID()}`, type: 'supplier', name, properties: { capabilities: [] }, customer_id: 'default' };
    const receipt = { action: 'create' as const, target }; setUncertainMutation(receipt);
    let failure = '';
    try {
      const saved = await readJson<KGNode>(await authFetch(`${API_BASE}/api/knowledge/nodes`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(target) }), "供应商创建失败");
      if (saved.id !== target.id) throw new Error("供应商创建结果无效");
    } catch (reason) { failure = reason instanceof Error ? reason.message : "供应商创建失败"; }
    try { await reconcileSupplier(receipt, failure); }
    finally { mutationLock.current = false; setMutation(""); }
  };
  const handleDeleteSupplier = async (supplier: KGNode) => {
    if (mutationLock.current || uncertainMutation) return;
    mutationLock.current = true; setMutation(supplier.id); setMutationError("");
    const receipt = { action: 'delete' as const, target: supplier }; setUncertainMutation(receipt);
    let failure = '';
    try {
      const response = await readJson<{ ok: boolean }>(await authFetch(`${API_BASE}/api/knowledge/nodes/${encodeURIComponent(supplier.id)}?customer_id=${encodeURIComponent(supplier.customer_id)}`, { method: "DELETE" }), "供应商删除失败");
      if (response.ok !== true) throw new Error("供应商删除未获确认");
    } catch (reason) { failure = reason instanceof Error ? reason.message : "供应商删除失败"; }
    try { await reconcileSupplier(receipt, failure); }
    finally { mutationLock.current = false; setMutation(""); }
  };
  const reconcileSupplier = async (receipt: NonNullable<typeof uncertainMutation>, failure = '') => {
    setMutation('verify');
    try {
      const response = await authFetch(`${API_BASE}/api/knowledge/nodes/${encodeURIComponent(receipt.target.id)}?customer_id=${encodeURIComponent(receipt.target.customer_id)}`);
      const actual = response.status === 404 ? null : await readJson<KGNode>(response, '供应商结果核对失败');
      if (actual && (actual.id !== receipt.target.id || actual.type !== 'supplier' || actual.customer_id !== receipt.target.customer_id)) throw new Error('供应商结果身份不匹配');
      const applied = receipt.action === 'delete' ? actual === null : actual?.name === receipt.target.name;
      if (applied) {
        setSuppliers(previous => actual ? [...previous.filter(item => item.id !== actual.id), actual] : previous.filter(item => item.id !== receipt.target.id));
        if (receipt.action === 'create') { setSupplierDialog(false); setSupplierName(''); }
      }
      setUncertainMutation(null);
      setMutationError(applied ? '' : `${failure ? failure + '；' : ''}已核对服务器，操作目标尚未生效，可以重试。`);
      await fetchSuppliers();
    } catch { setMutationError('操作结果未确认，请恢复连接后核对结果。'); }
    finally { setMutation(''); }
  };
  const feedback = (resource: Resource, retry: () => void) => <>
    {states[resource].pending ? <p role="status">加载中...</p> : null}
    {states[resource].error ? <div role="alert"><p className="text-red-700">{states[resource].error}</p><button className="workspace-button mt-2" disabled={states[resource].pending} onClick={retry} type="button">重试</button></div> : null}
  </>;
  if (!open) return <button className="ww-settings-entry" onClick={() => setOpen(true)} type="button">工艺知识图谱</button>;
  return <section className="space-y-3 p-4">
    <header className="flex items-center justify-between gap-2"><h4 className="type-section-heading">工艺知识图谱</h4>{!embedded ? <button className="workspace-button" type="button" onClick={() => setOpen(false)}>收起</button> : null}</header>
    <div className="flex flex-wrap gap-2">{([['processes', '工艺-材料'], ['recommend', '智能推荐'], ['suppliers', '供应商']] as const).map(([value, label]) => <button className="workspace-button" type="button" key={value} aria-pressed={tab === value} onClick={() => setTab(value)}>{label}</button>)}</div>
    {uncertainMutation && !supplierDialog ? <div role="status"><p>操作结果未确认，请核对后继续。</p><button className="workspace-button" type="button" disabled={Boolean(mutation)} onClick={() => void reconcileSupplier(uncertainMutation)}>核对结果</button></div> : null}
    {tab === "processes" ? <>
      {feedback("processes", () => void fetchProcesses())}
      <div className="flex flex-wrap gap-2">{processes.map(process => <button className="workspace-button" type="button" key={process.id} aria-pressed={activeProcess === process.id} onClick={() => setActiveProcess(process.id)}>{PROCESS_LABELS[process.id] || process.name}</button>)}</div>
      {feedback("materials", () => void fetchMaterials(activeProcess))}
      {materials.map(item => <article className="rounded border border-[var(--line)] p-3" key={item.material.id}><strong>{item.material.name}</strong><dl className="mt-2 grid gap-1 type-caption">{Object.entries(item.constraints).map(([key, value]) => <div key={key}><dt className="inline" data-i18n-skip>{key}: </dt><dd className="inline" data-i18n-skip>{String(value)}</dd></div>)}</dl></article>)}
      {!states.materials.pending && !states.materials.error && states.materials.loaded && !materials.length ? <p>无支持材料记录</p> : null}
      {!states.processes.pending && !states.processes.error && states.processes.loaded && !processes.length ? <p>暂无工艺记录</p> : null}
    </> : null}
    {tab === "recommend" ? <>
      <div className="ww-form-grid"><label>最大尺寸 (mm)<input type="number" min="0" step="any" value={recDim} onChange={event => setRecDim(event.target.value)} /></label><label>材料偏好<input value={recMat} onChange={event => setRecMat(event.target.value)} /></label>
        <button className="workspace-button workspace-button--primary" disabled={states.recommend.pending} onClick={handleRecommend} type="button">{states.recommend.pending ? "正在推荐" : "推荐"}</button></div>
      {feedback("recommend", handleRecommend)}
      {recommendations.map(item => <article className="rounded border border-[var(--line)] p-3" key={item.process_id}><strong>{item.process_name}</strong><span className="ml-2">{(item.score * 100).toFixed(0)}%</span>{item.material_name ? <p>材料：{item.material_name}</p> : null}{item.notes.map((note, index) => <p className="type-caption text-[var(--muted)]" key={index}>{note}</p>)}</article>)}
      {!states.recommend.pending && !states.recommend.error && states.recommend.loaded && !recommendations.length ? <p>暂无匹配推荐</p> : null}
    </> : null}
    {tab === "suppliers" ? <>
      {feedback("suppliers", () => void fetchSuppliers())}
      {suppliers.map(supplier => <article className="flex items-center justify-between gap-2 rounded border border-[var(--line)] p-3" key={supplier.id}><span data-i18n-skip>{supplier.name}</span><button className="workspace-button" disabled={Boolean(mutation) || Boolean(uncertainMutation)} type="button" onClick={() => void handleDeleteSupplier(supplier)}>{mutation === supplier.id ? "正在删除" : "删除"}</button></article>)}
      {!states.suppliers.pending && !states.suppliers.error && states.suppliers.loaded && !suppliers.length ? <p>暂无供应商记录</p> : null}
      {mutationError && !supplierDialog ? <p role="alert" className="text-red-700">{mutationError}</p> : null}
      <button className="workspace-button" disabled={Boolean(mutation) || Boolean(uncertainMutation)} type="button" onClick={() => { setMutationError(""); setSupplierDialog(true); }}>+ 添加供应商</button>
    </> : null}
    <WorkspaceDialog open={supplierDialog} title="添加供应商" onClose={() => { if (!mutation) setSupplierDialog(false); }} footer={<div className="flex justify-end gap-2"><button className="workspace-button" disabled={Boolean(mutation)} type="button" onClick={() => setSupplierDialog(false)}>取消</button><button className="workspace-button workspace-button--primary" disabled={!supplierName.trim() || Boolean(mutation) || Boolean(uncertainMutation)} type="button" onClick={() => void handleAddSupplier()}>{mutation ? "正在保存" : "保存"}</button></div>}>
      <div className="ww-form-grid p-4"><label>供应商名称<input maxLength={200} value={supplierName} onChange={event => setSupplierName(event.target.value)} disabled={Boolean(mutation) || Boolean(uncertainMutation)} /></label>{mutationError ? <p role="alert" className="text-red-700">{mutationError}</p> : null}{uncertainMutation ? <button className="workspace-button" type="button" disabled={Boolean(mutation)} onClick={() => void reconcileSupplier(uncertainMutation)}>核对结果</button> : null}</div>
    </WorkspaceDialog>
  </section>;
}
