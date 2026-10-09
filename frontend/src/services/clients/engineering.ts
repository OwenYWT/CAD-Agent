import { authFetch } from "../../auth";
import type { EngineeringTaskSummary, EngineeringResult, EngineeringField, EngineeringTask, CamToolpath } from "../../types/engineeringTask";
import { validateEngineeringField } from "../../adapters/engineeringField";
import { validateCamToolpath } from "../../adapters/camToolpath";
import type { CloudDocument } from "../../types/document";
import { API_BASE } from "./http";
import { readEngineeringFieldData } from "./artifacts";
import { readJson } from "./http";

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

export interface NativeMeasurementRequest {
  kind: 'native_measure'; component_name: string; measurement: 'volume' | 'solid_count' | 'face_distance' | 'circle_diameter'|'component_clearance'|'intersection_volume';other_component_name?:string;
  selectors: NonNullable<import('../../types/document').SelectionContext['topology_selector']>[];
}
export async function submitNativeMeasurement(document: CloudDocument, task: NativeMeasurementRequest, idempotencyKey: string) {
  return readJson<{workflow_run_id: string}>(await authFetch(`${API_BASE}/api/documents/${document.document_id}/engineering`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ task, idempotency_key: idempotencyKey,
      expected_revision_id: document.revision_id, expected_state_version: document.state_version }),
  }), '原生测量提交失败');
}
export async function readNativeMeasurement(documentId: string, taskId: string, signal?: AbortSignal) {
  return readJson<{ source_revision_id: string; report: { status: 'measured'; value: number; unit: string; method: string } }>(await authFetch(`${API_BASE}/api/documents/${documentId}/engineering/${taskId}`, { signal }), '原生测量读取失败');
}
