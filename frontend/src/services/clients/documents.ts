import { authFetch } from "../../auth";
import type { SketchDetails, SketchConstraint, SketchGeometry, SketchDimensionEdit } from "../../types/sketch";
import type { CloudDocument, DocumentRevisionView, DocumentCollaboration, SemanticFeature, KernelInspection, InspectionFieldName } from "../../types/document";
import { API_BASE } from "./http";
import { readJson } from "./http";

export async function getCloudDocument(id: string, signal?: AbortSignal): Promise<CloudDocument> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${encodeURIComponent(id)}`, { signal }), "文档加载失败");
}

export async function getDocumentRevisionView(documentId: string, revisionId: string, signal?: AbortSignal): Promise<DocumentRevisionView> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${documentId}/revisions/${revisionId}`, { signal }), "版本证据加载失败");
}

export async function createDocumentReviewLink(id: string, role: "viewer" | "editor" = "viewer"): Promise<string> {
  const invitation = await readJson<{ token: string; tenant_id: string }>(await authFetch(`${API_BASE}/api/documents/${encodeURIComponent(id)}/invitations?role=${role}`, { method: "POST" }), "邀请创建失败");
  const link = new URL(window.location.origin);
  link.searchParams.set("document", id); link.searchParams.set("workspace", invitation.tenant_id);
  link.hash = new URLSearchParams({ invite: invitation.token }).toString();
  return link.toString();
}

export async function acceptDocumentReviewLink(id: string, tenantId: string, token: string): Promise<void> {
  await readJson(await authFetch(`${API_BASE}/api/documents/${encodeURIComponent(id)}/invitations/accept`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tenant_id: tenantId, token }),
  }), "邀请已失效或无法接受");
}

export async function getDocumentCollaboration(id: string): Promise<DocumentCollaboration> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${encodeURIComponent(id)}/collaboration`), "协作信息加载失败");
}

export interface ProjectMember { principal_id: string; display_name: string; role: string }

export async function getDocumentMembers(id: string): Promise<ProjectMember[]> {
  const result = await readJson<{ members: ProjectMember[] }>(await authFetch(`${API_BASE}/api/documents/${id}/members`), "成员列表加载失败");
  return result.members;
}

export async function changeDocumentMember(id: string, member: ProjectMember, role: "viewer" | "editor" | null): Promise<void> {
  const path = `${API_BASE}/api/documents/${id}/members/${member.principal_id}`;
  const response = await authFetch(role ? path : `${path}?expected_role=${encodeURIComponent(member.role)}`, {
    method: role ? "PATCH" : "DELETE",
    ...(role ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_role: member.role, role }) } : {}),
  });
  if (!response.ok) await readJson(response, "成员权限更新失败");
}

export async function updateDocumentPresence(id: string, clientId: string, featureId: string | null): Promise<void> {
  const response = await authFetch(`${API_BASE}/api/documents/${encodeURIComponent(id)}/presence`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: clientId, selected_feature_id: featureId }),
  });
  if (!response.ok) await readJson(response, "在线状态同步失败");
}

export async function commentOnDocument(document: CloudDocument, body: string, featureId: string | null): Promise<void> {
  await readJson(await authFetch(`${API_BASE}/api/documents/${document.document_id}/comments`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comment_id: crypto.randomUUID(), revision_id: document.revision_id, body, feature_id: featureId }),
  }), "评论保存失败");
}

export async function annotateDocumentFeature(document: CloudDocument, feature: SemanticFeature,
  role: string, intent: string, annotationId: string): Promise<{ version: number }> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${document.document_id}/features/${feature.id}/annotation`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ annotation_id: annotationId, revision_id: document.head_revision_id,
      expected_version: feature.annotation_version || 0, role: role.trim() || null, intent: intent.trim() || null }),
  }), "特征标注保存失败");
}

export async function inspectDocumentFeature(document: CloudDocument, feature: SemanticFeature,
  field: InspectionFieldName, offset = 0): Promise<KernelInspection> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${document.document_id}/inspect`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision_id: document.revision_id,
      query: { objects: [feature.kernel_name], fields: [field], offset, limit: 16 } }),
  }), "内核细节读取失败");
}

export async function acquireDocumentFeatureLease(document: CloudDocument, featureId: string, clientId: string, leaseToken?: string): Promise<{ token: string }> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${document.document_id}/leases`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feature_id: featureId, client_id: clientId, revision_id: document.head_revision_id, lease_token: leaseToken || null }),
  }), "编辑租约获取失败");
}

export async function releaseDocumentFeatureLease(documentId: string, token: string): Promise<void> {
  const response = await authFetch(`${API_BASE}/api/documents/${documentId}/leases/${token}`, { method: "DELETE" });
  if (!response.ok) await readJson(response, "编辑租约释放失败");
}

export async function updateDocumentInstance(document: CloudDocument, action: "assembly.instance" | "assembly.place",
  args: Record<string, unknown>, idempotencyKey: string, token: string): Promise<{ workflow_run_id: string }> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${document.document_id}/operations`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "native.update", expected_base_revision_id: document.head_revision_id,
      expected_state_version: document.state_version, idempotency_key: idempotencyKey, lease_token: token,
      modification: { expected_state_sha256: document.parameter_state_sha256, native_edits: [{ action, args }] } }),
  }), "实例修改提交失败");
}

export async function readDocumentSketch(document: CloudDocument, feature: SemanticFeature, signal?: AbortSignal): Promise<SketchDetails> {
  async function field(name: "geometry" | "constraints") {
    const items: unknown[]=[];let total=0, recorded=0;
    do {
      const result=await readJson<KernelInspection>(await authFetch(`${API_BASE}/api/documents/${document.document_id}/inspect`, {
        method:"POST",signal,headers:{"Content-Type":"application/json"},body:JSON.stringify({revision_id:document.revision_id,
          query:{objects:[feature.kernel_name],fields:[name],offset:items.length,limit:32}}),
      }), "草图记录加载失败");
      if (result.artifact_sha256!==document.parameter_state_sha256) throw new Error("草图检查点校验值不一致");
      const page=result.objects[0]?.[name];
      if (page?.status!=="measured" || !page.items) throw new Error("此检查点未记录可预览的草图细节");
      total=page.total ?? 0;recorded=Math.min(256,page.recorded ?? 0);
      if (!page.items.length) break;
      items.push(...page.items);
    } while (items.length<recorded);
    return {items,omitted:Math.max(0,total-items.length)};
  }
  const [geometry,constraints]=await Promise.all([field("geometry"),field("constraints")]);
  return {geometry:geometry.items as SketchGeometry[],constraints:constraints.items as SketchConstraint[],
    omitted_geometry:geometry.omitted,omitted_constraints:constraints.omitted};
}

export async function updateSketchDimensions(document: CloudDocument, feature: SemanticFeature, edits: SketchDimensionEdit[], key: string, token: string) {
  return readJson<{workflow_run_id:string}>(await authFetch(`${API_BASE}/api/documents/${document.document_id}/operations`, {
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"native.update",
      expected_base_revision_id:document.head_revision_id,expected_state_version:document.state_version,idempotency_key:key,lease_token:token,
      objective:`修改草图 ${feature.label} 的尺寸约束`,modification:{expected_state_sha256:document.parameter_state_sha256,
        native_edits:edits.map(edit=>({action:"sketch.set_constraint",args:{sketch:feature.kernel_name,...edit}}))}}),
  }), "草图约束提交失败");
}

export async function updateDocumentParameters(document: CloudDocument, updates: Array<{ parameter_id: string; value: number }>, idempotencyKey: string, leaseToken?: string): Promise<{ workflow_run_id: string }> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${document.document_id}/operations`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "parameters.update", expected_base_revision_id: document.head_revision_id,
      expected_state_version: document.state_version, idempotency_key: idempotencyKey,
      lease_token: leaseToken || null, allow_rebase: false,
      modification: { expected_state_sha256: document.parameter_state_sha256, parameter_updates: updates } }),
  }), "参数修改提交失败");
}

export async function deleteHistoryProject(sessionId: string): Promise<void> {
  const response = await authFetch(`${API_BASE}/api/history/sessions/${sessionId}`, { method: "DELETE" });
  if (!response.ok) throw new Error("删除项目失败，请重试");
}
