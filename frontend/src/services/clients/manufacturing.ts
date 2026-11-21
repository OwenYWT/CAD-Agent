import type { DFMRuleData } from "../../types/dfm";
import { API_BASE, authFetch, readJson } from "./http";

export interface RuleSet { id: string; name: string; process: string; rules: DFMRuleData[] }
export interface KGNode { id: string; type: string; name: string; properties: Record<string, unknown>; customer_id: string }
export interface ProcessRecommendation { process_id: string; process_name: string; material_id: string | null; material_name: string | null; constraints: Record<string, unknown>; score: number; notes: string[] }
export interface MaterialInfo { material: KGNode; constraints: Record<string, unknown> }

export async function loadRuleSets(signal?: AbortSignal): Promise<RuleSet[]> {
  return readJson(await authFetch(`${API_BASE}/api/dfm/rules`, { signal }), "规则加载失败");
}

export async function updateRule(id: string, body: Record<string, number | boolean>): Promise<DFMRuleData> {
  return readJson(await authFetch(`${API_BASE}/api/dfm/rules/${encodeURIComponent(id)}`, {
    method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  }), "规则保存失败");
}

export async function listNodes(type: "process" | "supplier", signal: AbortSignal): Promise<KGNode[]> {
  return readJson(await authFetch(`${API_BASE}/api/knowledge/nodes?type=${type}`, {signal}), "节点读取失败");
}

export async function loadMaterials(process: string, signal: AbortSignal): Promise<MaterialInfo[]> {
  return readJson(await authFetch(`${API_BASE}/api/knowledge/process/${encodeURIComponent(process)}/materials`, {signal}), "材料读取失败");
}

export async function recommendProcesses(dimension: number | null, material: string | null, signal: AbortSignal): Promise<ProcessRecommendation[]> {
  return readJson(await authFetch(`${API_BASE}/api/knowledge/recommend`, {method: "POST", signal,
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({max_dimension: dimension, material})}), "工艺推荐失败");
}

export async function createSupplier(node: KGNode): Promise<KGNode> {
  return readJson(await authFetch(`${API_BASE}/api/knowledge/nodes`, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(node),
  }), "供应商创建失败");
}

function supplierUrl(node: KGNode): string {
  return `${API_BASE}/api/knowledge/nodes/${encodeURIComponent(node.id)}?customer_id=${encodeURIComponent(node.customer_id)}`;
}

export async function findSupplier(node: KGNode): Promise<KGNode | null> {
  const response = await authFetch(supplierUrl(node));
  return response.status === 404 ? null : readJson(response, "供应商结果核对失败");
}

export async function deleteSupplier(node: KGNode): Promise<{ok: boolean}> {
  return readJson(await authFetch(supplierUrl(node), {method: "DELETE"}), "供应商删除失败");
}
