import type { WSMessage } from "../types";
import type { useSessionStore } from "../stores/sessionStore";
import { pendingSubmission, acknowledgeSubmission } from "../stores/pendingSubmissions.ts";

/** Apply messages to the server-addressed panel, never whichever panel is active. */
export function dispatchSessionMessage(msg: WSMessage, sessionId: string,
  store: Pick<ReturnType<typeof useSessionStore.getState>, "ownerId" | "setRunCreated" | "setDurableWorkflowStarted" | "setStep" | "setAgentStep" | "addArtifactUpdate" | "setResult" | "setError">,
  send: (data: string) => void) {
  const { setRunCreated, setDurableWorkflowStarted, setStep, setAgentStep, addArtifactUpdate, setResult, setError } = store;
          if (msg.type === "run_created") {
            setRunCreated(msg.data, msg.data.panel_id);
          } else if (msg.type === "task_submitted") {
            const pending = pendingSubmission(store.ownerId, sessionId, msg.data.panel_id);
            if (pending && msg.data.submission_id !== pending.idempotency_key) return;
            acknowledgeSubmission(store.ownerId, sessionId, msg.data.panel_id, msg.data.submission_id);
            setDurableWorkflowStarted(msg.data, msg.data.panel_id);
          } else if (msg.type === "submission_not_found") {
            const pending = pendingSubmission(store.ownerId, sessionId, msg.data.panel_id);
            if (pending?.idempotency_key === msg.data.submission_id) send(JSON.stringify(pending));
          } else if (msg.type === "step_update") {
            setStep(msg.data, msg.data.panel_id);
          } else if (msg.type === "agent_step") {
            setAgentStep(msg.data, msg.data.panel_id);
          } else if (msg.type === "artifact_update") {
            addArtifactUpdate(msg.data, msg.data.panel_id);
          } else if (msg.type === "generation_result") {
            if (msg.data.submission_id && msg.data.panel_id) {
              const pending = pendingSubmission(store.ownerId,sessionId,msg.data.panel_id);
              if (pending && pending.idempotency_key !== msg.data.submission_id) return;
              if (msg.data.submission_outcome === "rejected") acknowledgeSubmission(store.ownerId, sessionId, msg.data.panel_id, msg.data.submission_id);
              else if (pending) {
                setError("任务是否受理尚未确认，原始请求已保留。请查询原请求状态后继续。",msg.data.panel_id);
                return;
              }
            }
            setResult(msg.data, msg.data.panel_id);
          } else if (msg.type === "task_status" && msg.data.status === "not_found") {
            setError("未找到可恢复的任务，请重新提交", msg.data.panel_id);
          }
}
