import { authFetch } from "../../auth";
import type { DocumentBranches, BranchComparison } from "../../types/branches";
import type { CloudDocument } from "../../types/document";
import { API_BASE } from "./http";
import { readJson } from "./http";

export async function getDocumentBranches(id: string, signal?: AbortSignal): Promise<DocumentBranches> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${id}/branches`, {signal}), "分支加载失败");
}

export async function createDocumentBranch(document: CloudDocument, name: string, idempotencyKey: string) {
  return readJson<{document_id: string; tenant_id: string; workflow_run_id: string}>(await authFetch(`${API_BASE}/api/documents/${document.document_id}/branches`, {
    method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({name, idempotency_key:idempotencyKey,
      expected_revision_id:document.head_revision_id, expected_state_version:document.state_version}),
  }), "分支创建失败");
}

export async function compareDocumentBranches(id: string, sourceId: string): Promise<BranchComparison> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${id}/compare/${sourceId}`), "分支比较失败");
}

export async function mergeDocumentBranch(comparison: BranchComparison, mode: "parameters" | "source_geometry") {
  const {source_document_id, source_revision_id, source_state_version, target_revision_id, target_state_version, comparison_hash} = comparison;
  return readJson<{workflow_run_id: string}>(await authFetch(`${API_BASE}/api/documents/${comparison.document_id}/merges`, {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({source_document_id,source_revision_id,
      source_state_version,target_revision_id,target_state_version,comparison_hash,mode,idempotency_key:`${mode}:${comparison_hash}`}),
  }), "合并提交失败");
}
