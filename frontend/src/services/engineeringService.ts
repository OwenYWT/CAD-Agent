import { authFetch } from "../auth";
import type { EngineeringTaskSummary, EngineeringResult, EngineeringField, EngineeringTask, CamToolpath } from '../types/engineeringTask';
import { validateEngineeringField } from '../adapters/engineeringField';
import { validateCamToolpath } from '../adapters/camToolpath';
import type { DocumentRelease, ReleaseArtifact, ReleaseBOM, ReleaseResult } from '../types/release';
import type { BridgeState, BridgePairing } from '../types/localBridge';
import type { DocumentBranches, BranchComparison } from "../types/branches";
import type { SketchDetails, SketchConstraint, SketchGeometry, SketchDimensionEdit } from "../types/sketch";
import type { CloudDocument, DocumentRevisionView, DocumentCollaboration, SemanticFeature, KernelInspection, InspectionFieldName } from "../types/document";
import type {
  DesignAnalysis,
  DurableTaskEvent,
  DurableTaskSnapshot,
  GenerationResult,
  FreeCADBOMDocument,
  ModelSnapshotDiff,
  ModelSnapshotDetail,
  ModelSnapshotSummary,
} from "../types";
import type {
  ChangeSetActionResult,
  DurableChangeSetDetail,
  HistoryProject,
  OnshapeConfig,
  OnshapeLink,
} from "../types/engineering";

const API_BASE = import.meta.env.VITE_API_BASE || "";

export async function listEngineeringTasks(documentId: string, signal?: AbortSignal): Promise<EngineeringTaskSummary[]> {
  const result = await readJson<{tasks: EngineeringTaskSummary[]}>(await authFetch(`${API_BASE}/api/documents/${documentId}/engineering`, {signal}), '工程任务加载失败');
  return result.tasks;
}

export async function submitEngineeringTask(document: CloudDocument, task: EngineeringTask, idempotencyKey: string) {
  return readJson<{workflow_run_id: string}>(await authFetch(`${API_BASE}/api/documents/${document.document_id}/engineering`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({task, idempotency_key: idempotencyKey,
      expected_revision_id: document.head_revision_id, expected_state_version: document.state_version}),
  }), '工程计算提交失败');
}

export async function readEngineeringResult(documentId: string, taskId: string, signal?: AbortSignal): Promise<EngineeringResult> {
  return readJson(await authFetch(`${API_BASE}/api/documents/${documentId}/engineering/${taskId}`, {signal}), '工程结果加载失败');
}

export async function readEngineeringField(ref: EngineeringResult['artifacts']['engineering_field'], signal?: AbortSignal): Promise<EngineeringField> {
  return validateEngineeringField(await readEngineeringFieldData(ref,signal) as EngineeringField);
}

export async function readCamToolpath(ref: EngineeringResult['artifacts']['engineering_field'], signal?: AbortSignal): Promise<CamToolpath> {
  return validateCamToolpath(await readEngineeringFieldData(ref,signal) as CamToolpath);
}

async function readVerifiedEngineeringBytes(ref: EngineeringResult['artifacts']['engineering_field'], signal?: AbortSignal): Promise<ArrayBuffer> {
  const response = await authFetch(`${API_BASE}${ref.url}`, {signal});
  if (!response.ok) throw new Error(`工程场数据读取失败（HTTP ${response.status}）`);
  const bytes = await response.arrayBuffer();
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2,'0')).join('');
  if (bytes.byteLength !== ref.size_bytes || hash !== ref.sha256) throw new Error('工程场数据完整性校验失败');
  return bytes;
}

async function readEngineeringFieldData(ref: EngineeringResult['artifacts']['engineering_field'], signal?: AbortSignal): Promise<unknown> {
  return JSON.parse(new TextDecoder().decode(await readVerifiedEngineeringBytes(ref,signal)));
}

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

export async function submitDocumentRelease(document:CloudDocument, releaseName:string, engineeringWorkflowIds:string[], key:string) {
  return readJson<{release_id:string}>(await authFetch(`${API_BASE}/api/documents/${document.document_id}/releases`,{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({release_name:releaseName,
      expected_revision_id:document.head_revision_id,expected_state_version:document.state_version,
      engineering_workflow_ids:engineeringWorkflowIds,idempotency_key:key}),
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

export async function cancelEngineeringTask(taskId: string): Promise<void> {
  await readJson(await authFetch(`${API_BASE}/api/tasks/${taskId}/cancel`, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reason:'用户取消工程计算'})}), '取消工程任务失败');
}

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

interface PanelSummary {
  id: string;
  title: string;
  current_code: string | null;
  project_id?: string | null;
  branch_id?: string | null;
  current_revision_id?: string | null;
  active_workflow_run_id?: string | null;
  active_workflow_status?: string | null;
}

interface MessageData {
  role: "user" | "assistant";
  content: string;
  result?: GenerationResult;
}

export interface RestoredProject {
  sessionId: string;
  panels: Array<{
    id: string;
    title: string;
    messages: MessageData[];
    currentCode: string | null;
    projectId: string | null;
    branchId: string | null;
    currentRevisionId: string | null;
    workflowRunId: string | null;
    workflowStatus: string | null;
    changeSetId: string | null;
    taskSnapshot: DurableTaskSnapshot | null;
  }>;
}

function apiErrorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
    if (record.detail) return apiErrorMessage(record.detail, fallback);
  }
  return fallback;
}

export class EngineeringApiError extends Error {
  code: string | null;
  retryable: boolean;
  httpStatus?: number;

  constructor(message: string, code: string | null, retryable = false, httpStatus?: number) {
    super(message);
    this.name = "EngineeringApiError";
    this.code = code;
    this.retryable = retryable;
    this.httpStatus = httpStatus;
  }
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown };
    const nested = body.detail && typeof body.detail === "object"
      ? body.detail as Record<string, unknown>
      : null;
    throw new EngineeringApiError(
      apiErrorMessage(body.detail, fallback),
      typeof nested?.code === "string" ? nested.code : null,
      nested?.retryable === true,
      response.status,
    );
  }
  return response.json() as Promise<T>;
}

export async function listHistoryProjects(): Promise<HistoryProject[]> {
  const response = await authFetch(`${API_BASE}/api/history/sessions`);
  const rows = await readJson<Array<{
    id: string;
    project_id?: string | null;
    title: string;
    updated_at: string;
  }>>(response, "历史项目加载失败");
  return rows.map((row) => ({
    id: row.id,
    projectId: row.project_id || null,
    name: row.title || `项目 ${row.id.slice(0, 8)}`,
    updatedAt: row.updated_at,
  }));
}

export async function restoreHistoryProject(sessionId: string): Promise<RestoredProject> {
  const panelResponse = await authFetch(`${API_BASE}/api/history/sessions/${sessionId}/panels`);
  const panels = await readJson<PanelSummary[]>(panelResponse, "项目面板加载失败");
  const hydrated = await Promise.all(panels.map(async (panel) => {
    const workflowRunId = panel.active_workflow_run_id || null;
    const [messages, taskSnapshot] = await Promise.all([
      authFetch(`${API_BASE}/api/history/panels/${panel.id}/messages`).then(
        (response) => readJson<MessageData[]>(response, "项目消息加载失败"),
      ),
      workflowRunId
        ? getDurableTaskSnapshot(workflowRunId)
        : Promise.resolve(null),
    ]);
    return {
      id: panel.id,
      title: panel.title || "工程任务",
      messages,
      currentCode: panel.current_code,
      projectId: panel.project_id || null,
      branchId: panel.branch_id || null,
      currentRevisionId: panel.current_revision_id || null,
      workflowRunId,
      workflowStatus: taskSnapshot?.status
        || panel.active_workflow_status
        || null,
      changeSetId: taskSnapshot?.change_set?.id || null,
      taskSnapshot,
    };
  }));
  return { sessionId, panels: hydrated };
}

export interface DurableTaskEventPage {
  workflow_run_id: string;
  events: DurableTaskEvent[];
  after_sequence: number;
  next_cursor: number;
  earliest_sequence: number;
  current_sequence: number;
  has_more: boolean;
}

export async function getDurableTaskSnapshot(
  workflowRunId: string,
): Promise<DurableTaskSnapshot> {
  const response = await authFetch(
    `${API_BASE}/api/tasks/${encodeURIComponent(workflowRunId)}/snapshot`,
  );
  return readJson<DurableTaskSnapshot>(response, "任务状态加载失败");
}

export async function listDurableTaskEvents(
  workflowRunId: string,
  afterSequence: number,
): Promise<DurableTaskEventPage> {
  const query = new URLSearchParams({
    after_sequence: String(afterSequence),
    limit: "100",
  });
  const response = await authFetch(
    `${API_BASE}/api/tasks/${encodeURIComponent(workflowRunId)}/events?${query}`,
  );
  return readJson<DurableTaskEventPage>(response, "任务事件加载失败");
}

export interface DurableConfirmationResult {
  workflow_run_id: string;
  accepted: boolean;
  status: "signal_delivered";
}

export async function confirmDurableTask(
  workflowRunId: string,
  accepted: boolean,
  note = "",
): Promise<DurableConfirmationResult> {
  const response = await authFetch(
    `${API_BASE}/api/tasks/${encodeURIComponent(workflowRunId)}/confirmation`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accepted, note }),
    },
  );
  return readJson<DurableConfirmationResult>(response, "任务确认失败");
}

export async function getRevisionBOM(
  projectId: string,
  revisionId: string,
  signal?: AbortSignal,
): Promise<FreeCADBOMDocument> {
  const response = await authFetch(
    `${API_BASE}/api/projects/${encodeURIComponent(projectId)}`
      + `/revisions/${encodeURIComponent(revisionId)}/bom`,
    { signal },
  );
  return readJson<FreeCADBOMDocument>(response, "BOM 加载失败");
}

export async function getDurableChangeSet(
  changeSetId: string,
): Promise<DurableChangeSetDetail> {
  const response = await authFetch(
    `${API_BASE}/api/change-sets/${encodeURIComponent(changeSetId)}`,
  );
  return readJson<DurableChangeSetDetail>(response, "变更审查加载失败");
}

async function changeSetAction(
  changeSetId: string,
  action: "accept" | "reject" | "request-change" | "commit" | "rollback",
  note?: string,
): Promise<ChangeSetActionResult> {
  const response = await authFetch(
    `${API_BASE}/api/change-sets/${encodeURIComponent(changeSetId)}/${action}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: action === "commit" ? undefined : JSON.stringify({ note: note || "" }),
    },
  );
  return readJson<ChangeSetActionResult>(response, "变更操作失败");
}

export const acceptDurableChangeSet = (id: string, note = "") =>
  changeSetAction(id, "accept", note);
export const rejectDurableChangeSet = (id: string, note: string) =>
  changeSetAction(id, "reject", note);
export const requestDurableChangeSetModification = (id: string, note: string) =>
  changeSetAction(id, "request-change", note);
export const commitDurableChangeSet = (id: string) =>
  changeSetAction(id, "commit");
export const rollbackDurableChangeSet = (id: string, note = "") =>
  changeSetAction(id, "rollback", note);

export async function deleteHistoryProject(sessionId: string): Promise<void> {
  const response = await authFetch(`${API_BASE}/api/history/sessions/${sessionId}`, { method: "DELETE" });
  if (!response.ok) throw new Error("删除项目失败，请重试");
}

export async function listModelSnapshots(
  panelId: string,
): Promise<ModelSnapshotSummary[]> {
  const response = await authFetch(
    `${API_BASE}/api/history/panels/${encodeURIComponent(panelId)}/snapshots`,
  );
  return readJson<ModelSnapshotSummary[]>(response, "版本列表加载失败");
}

export async function getModelSnapshot(
  snapshotId: string,
): Promise<ModelSnapshotDetail> {
  const response = await authFetch(
    `${API_BASE}/api/history/snapshots/${encodeURIComponent(snapshotId)}`,
  );
  return readJson<ModelSnapshotDetail>(response, "版本证据加载失败");
}

export async function diffModelSnapshots(
  fromSnapshotId: string,
  toSnapshotId: string,
): Promise<ModelSnapshotDiff> {
  const response = await authFetch(
    `${API_BASE}/api/history/snapshots/${encodeURIComponent(fromSnapshotId)}/diff/${encodeURIComponent(toSnapshotId)}`,
  );
  return readJson<ModelSnapshotDiff>(response, "版本对比加载失败");
}

export async function analyzeEngineeringResult(requestId: string, code: string, description: string): Promise<DesignAnalysis> {
  const response = await authFetch(`${API_BASE}/api/analyze/${encodeURIComponent(requestId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, description }),
  });
  return readJson<DesignAnalysis>(response, "工程检查失败");
}

export async function downloadEngineeringArtifact(url: string, filename: string): Promise<void> {
  const external = /^https?:\/\//.test(url);
  const target = external ? url : `${API_BASE}${url}`;
  // S3-compatible presigned URLs already carry short-lived authorization in
  // their query string. Sending the WordsWave bearer token to that host both
  // leaks credentials and can invalidate the object-store signature.
  const response = external ? await fetch(target) : await authFetch(target);
  if (!response.ok) throw new Error(`文件下载失败（HTTP ${response.status}）`);
  const objectUrl = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

export async function getOnshapeConfig(): Promise<OnshapeConfig> {
  const response = await authFetch(`${API_BASE}/api/onshape/config`);
  return readJson<OnshapeConfig>(response, "Onshape 配置状态加载失败");
}

export async function listOnshapeLinks(requestId: string): Promise<OnshapeLink[]> {
  const response = await authFetch(`${API_BASE}/api/onshape/links/${encodeURIComponent(requestId)}`);
  return readJson<OnshapeLink[]>(response, "Onshape 发布记录加载失败");
}

export async function publishToOnshape(requestId: string, stepFilename: string): Promise<OnshapeLink> {
  const response = await authFetch(`${API_BASE}/api/onshape/publish`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      request_id: requestId,
      step_filename: stepFilename,
      wait_for_completion: false,
    }),
  });
  return readJson<OnshapeLink>(response, "发布到 Onshape 失败");
}

export async function refreshOnshapeLink(requestId: string): Promise<OnshapeLink> {
  const response = await authFetch(`${API_BASE}/api/onshape/links/${encodeURIComponent(requestId)}/refresh`, {
    method: "POST",
  });
  return readJson<OnshapeLink>(response, "Onshape 导入状态刷新失败");
}

export async function retryDurableTask(workflowRunId:string,idempotencyKey:string) {
  const response=await authFetch(`${API_BASE}/api/tasks/${encodeURIComponent(workflowRunId)}/retry`,{
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({idempotency_key:idempotencyKey}),
  });
  return readJson<{workflow_run_id:string;project_id:string;branch_id:string;expected_base_revision_id:string;status:string}>(response,"重试提交失败");
}

export interface TaskValidationEvidence {
  evidence_id:string; evidence_hash:string; workflow_run_id:string;
  staging_manifest_id:string; revision_id:string | null; selected_for_revision:boolean;
  gate:string; mode:string; outcome:string; report:Record<string,unknown>;
}

export async function getTaskValidationEvidence(taskId:string,evidenceId:string) {
  return readJson<TaskValidationEvidence>(await authFetch(
    `${API_BASE}/api/tasks/${encodeURIComponent(taskId)}/validations/${encodeURIComponent(evidenceId)}`,
  ),'检查证据读取失败');
}
