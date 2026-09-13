import { authFetch } from "../auth";
import type { DocumentScene } from "../types/scene";
const API = import.meta.env.VITE_API_BASE || "";

export class SceneJobError extends Error {
  workflowId: string;
  constructor(message: string, workflowId: string) { super(message); this.workflowId = workflowId; }
}

function waitForPoll(signal: AbortSignal) {
  return new Promise<void>((resolve,reject) => {
    if (signal.aborted) { reject(new DOMException("Aborted","AbortError")); return; }
    const timer = setTimeout(() => { signal.removeEventListener("abort",abort); resolve(); },1000);
    const abort = () => { clearTimeout(timer); reject(new DOMException("Aborted","AbortError")); };
    signal.addEventListener("abort",abort,{once:true});
  });
}

export async function readDocumentScene(documentId: string, revisionId: string, signal: AbortSignal,
  onProgress: (message: string) => void, retryWorkflowId?: string): Promise<DocumentScene> {
  const url = `${API}/api/documents/${documentId}/scenes/${revisionId}`;
  let retry = retryWorkflowId;
  while (!signal.aborted) {
    const response = await authFetch(retry ? url + "/retry" : url, retry
      ? {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({workflow_run_id:retry}),signal} : {signal});
    retry = undefined;
    if (!response.ok) throw new Error(`部件场景读取失败 (${response.status})`);
    const result = await response.json();
    if (result.document_id !== documentId || result.revision_id !== revisionId) throw new Error("部件场景版本不匹配");
    if (response.status === 200) {
      if (result.schema_version !== "cad-scene.v1") throw new Error("部件场景格式不受支持");
      return result as DocumentScene;
    }
    if (response.status !== 202 || typeof result.workflow_run_id !== "string") throw new Error("缺少持久场景任务状态");
    if (["failed","cancelled","timed_out"].includes(result.status)) {
      throw new SceneJobError(result.error_message || "场景任务已终止，可重新计算；模型版本未改变。",result.workflow_run_id);
    }
    const statuses: Record<string,string> = {pending:"场景计算已排队",planning:"正在准备场景",running:"正在计算部件网格",cancelling:"场景计算正在取消"};
    if (!(result.status in statuses)) throw new Error("场景任务状态无法确认，请重新读取。");
    onProgress(`${statuses[result.status]} · ${result.workflow_run_id.slice(0,8)}`);
    await waitForPoll(signal);
  }
  throw new DOMException("Aborted","AbortError");
}
