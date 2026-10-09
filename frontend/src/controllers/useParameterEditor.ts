import { useState } from "react";
import { parameterSubmissionState } from "../adapters/parameterSubmission";
import { useDraftGuard } from "../hooks/useDraftGuard";
import { useFeatureLease } from "../hooks/useFeatureLease";
import type { CloudDocument, SemanticFeature, DocumentOperation } from "../types/document";
import type { DurablePanelContext } from "../stores/sessionStore";
import { confirmDurableTask } from "../services/clients/tasks";
import { updateDocumentParameters } from "../services/clients/documents";
import { EngineeringApiError } from "../services/clients/http";
import { useDraftHistory } from '../hooks/useDraftHistory';
export const PARAMETER_STATUS: Record<string, string> = {queued:"排队中", running:"运行中", reviewable:"等待审核", committed:"已提交", rejected:"已拒绝", failed:"失败", cancelled:"已取消", rolled_back:"已回退"};

/** One mounted edit session; the owner supplies its task context explicitly. */
export function useParameterEditor({feature, document, onSubmitted, operations = [], durable}: {
  feature: SemanticFeature; document: CloudDocument; operations?: DocumentOperation[];
  onSubmitted: (id: string, document: CloudDocument) => void; durable?: DurablePanelContext;
}) {
  const history = useDraftHistory<Record<string,string>>({});
  const values=history.value, setValues=history.set;
  const [editBase, setEditBase] = useState<CloudDocument | null>(null);
  const lease = useFeatureLease(document, feature.id);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState(() => crypto.randomUUID());
  const [submittedId, setSubmittedId] = useState<string | null>(null);
  const [attemptToken, setAttemptToken] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const confirmation = durable?.workflowRunId === submittedId && durable.taskStatus === 'waiting_confirmation' ? durable.confirmation : null;
  const [confirming, setConfirming] = useState(false);
  const confirm = async (accepted:boolean) => {
    if (!confirmation || confirming) return;
    setConfirming(true); setError("");
    try { await confirmDurableTask(confirmation.workflow_run_id, accepted, accepted ? '' : '用户拒绝当前参数执行计划'); }
    catch(e){ setError(e instanceof Error ? e.message : "执行计划确认失败"); }
    finally {setConfirming(false);}
  };
  const [savedMessage, setSavedMessage] = useState("");
  const {operation: submittedOperation, saved, failed} = parameterSubmissionState(submittedId, operations, document);
  // Update only from this request's authoritative operation + corresponding head,
  // never from a different task succeeding or from a changed parameter value.
  if (submittedId && (saved || failed)) {
    setSubmittedId(null); setAttemptToken(null); setUncertain(false); setIdempotencyKey(crypto.randomUUID());
    if (saved) {
      history.reset({}); setEditBase(null); setError(""); setSavedMessage(`已保存修订 ${document.head_revision_id.slice(0,8)}，可继续编辑`);
    } else {
      setError(`本次参数修改${PARAMETER_STATUS[submittedOperation!.status] || submittedOperation!.status}，输入已保留，可修正后重新提交。${submittedOperation?.error_code || ""}`);
    }
  }
  const originalFeature = editBase?.features.find((f) => f.id === feature.id) || feature;
  const stale = Boolean(editBase && (editBase.head_revision_id !== document.head_revision_id || editBase.state_version !== document.state_version));
  const updates = originalFeature.parameters.filter((p) => p.editable && values[p.id] !== undefined && Number(values[p.id]) !== p.value)
    .map((p) => ({ parameter_id: p.id, value: Number(values[p.id]) }));
  const valid = updates.length > 0 && originalFeature.parameters.every(p => values[p.id] === undefined || (
    !!values[p.id].trim() && Number.isFinite(Number(values[p.id]))
    && (p.minimum === null || Number(values[p.id]) >= p.minimum)
    && (p.maximum === null || Number(values[p.id]) <= p.maximum)));
  const dirty = Object.keys(values).length > 0 && !submittedId;
  const reset = () => { history.reset({}); setEditBase(null); setSubmittedId(null); setAttemptToken(null); setUncertain(false); setError(""); setIdempotencyKey(crypto.randomUUID()); void lease.release().catch(() => {}); };
  useDraftGuard(dirty || pending, `${feature.label} 参数`, reset);
  const submit = async () => {
    if (!valid || (stale && !uncertain) || submittedId || !document.can_edit) return;
    setPending(true); setError(""); setSavedMessage("");
    try {
      const token = attemptToken || await lease.ensure();
      setAttemptToken(token);
      const task = await updateDocumentParameters(editBase || document, updates, idempotencyKey, token);
      setSubmittedId(task.workflow_run_id); setUncertain(false);
      onSubmitted(task.workflow_run_id, editBase || document);
      void lease.release().catch(() => {});
    } catch (e) {
      const definitive = e instanceof EngineeringApiError && e.httpStatus !== undefined && e.httpStatus < 500;
      if (definitive) setAttemptToken(null);
      setUncertain(!definitive); setError(e instanceof Error ? e.message : "修改提交失败");
    }
    finally { setPending(false); }
  };
  const undo=()=>{history.undo();setIdempotencyKey(crypto.randomUUID());};
  const redo=()=>{history.redo();setIdempotencyKey(crypto.randomUUID());};
  return { values, setValues, editBase, setEditBase, lease, pending, error, setError, setIdempotencyKey, submittedId, uncertain, confirmation, confirming, confirm, savedMessage, setSavedMessage, submittedOperation, originalFeature, stale, valid, dirty, reset, submit,undo,redo,canUndo:history.canUndo,canRedo:history.canRedo };
}
