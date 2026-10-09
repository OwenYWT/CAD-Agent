import { authFetch } from "../../auth";
import type { DocumentRelease, ReleaseArtifact, ReleaseBOM, ReleaseResult, ReleaseOptions } from "../../types/release";
import type { BridgeState, BridgePairing } from "../../types/localBridge";
import type { CloudDocument } from "../../types/document";
import { API_BASE } from "./http";
import { readVerifiedEngineeringBytes } from "./artifacts";
import { readEngineeringFieldData } from "./artifacts";
import { readJson } from "./http";
import { downloadEngineeringArtifact } from "./downloads";

export async function listDocumentReleases(documentId:string, signal?:AbortSignal):Promise<DocumentRelease[]> {
  return (await readJson<{releases:DocumentRelease[]}>(await authFetch(`${API_BASE}/api/documents/${documentId}/releases`,{signal}),'发布列表加载失败')).releases;
}

export async function getLocalBridges(documentId:string,signal?:AbortSignal):Promise<BridgeState> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${documentId}/bridges`,{signal}),'本地连接加载失败');
}

export async function createLocalBridgePairing(documentId:string,label:string):Promise<BridgePairing> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${documentId}/bridges/pair`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label})}),'配对创建失败');
}

export async function revokeLocalBridge(documentId:string,bridgeId:string):Promise<void> {
  const response=await authFetch(`${API_BASE}/api/documents/${documentId}/bridges/${bridgeId}`,{method:'DELETE'});
  if (!response.ok) await readJson(response,'本地连接撤销失败');
}

export async function deliverLocalRelease(documentId:string,releaseId:string,bridgeId:string) {
  return readJson<{delivery_id:string;replayed:boolean}>(await authFetch(`${API_BASE}/api/documents/${documentId}/releases/${releaseId}/deliveries`,{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bridge_id:bridgeId}),
  }),'本地交付提交失败');
}

export async function downloadLocalBridgeClient():Promise<void> {
  await downloadEngineeringArtifact('/api/local-bridge/client','cad_local_bridge.py');
}

export async function submitDocumentRelease(document:CloudDocument, releaseName:string, engineeringWorkflowIds:string[], key:string, options?:ReleaseOptions) {
  return readJson<{release_id:string}>(await authFetch(`${API_BASE}/api/documents/${document.document_id}/releases`,{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({release_name:releaseName,
      expected_revision_id:document.head_revision_id,expected_state_version:document.state_version,
      engineering_workflow_ids:engineeringWorkflowIds,idempotency_key:key,...(options ? {options} : {})}),
  }),'发布提交失败');
}

export async function readDocumentRelease(documentId:string, releaseId:string, signal?:AbortSignal):Promise<ReleaseResult> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${documentId}/releases/${releaseId}`,{signal}),'发布详情加载失败');
}

export async function readReleaseBOM(ref:ReleaseArtifact, signal?:AbortSignal):Promise<ReleaseBOM> {
  const bom=await readEngineeringFieldData(ref,signal) as ReleaseBOM;
  if (bom.schema_version!=='cad-release-bom.v1' || bom.generator?.native_type!=='Assembly::BomObject' || !Array.isArray(bom.rows)) throw new Error('发布 BOM 格式无效');
  return bom;
}

export async function downloadReleaseArtifact(ref:ReleaseArtifact):Promise<void> {
  const bytes=await readVerifiedEngineeringBytes(ref);
  const url=URL.createObjectURL(new Blob([bytes]));const anchor=document.createElement('a');
  anchor.href=url;anchor.download=ref.filename;anchor.click();window.setTimeout(()=>URL.revokeObjectURL(url),0);
}
