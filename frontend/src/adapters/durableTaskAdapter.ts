import { createId } from "../lib/createId.ts";
import type {
  DurableTaskEvent,
  DurableTaskSnapshot,
  GenerationResult,
  StepUpdate,
} from "../types";
import type { DurableChangeSetDetail } from "../types/engineering";


export interface DurableWriteContext {
  projectId: string | null;
  branchId: string | null;
  currentRevisionId: string | null;
  stateVersion?: number;
}

export function durableWriteIdentity(context: DurableWriteContext, document?: {
  head_revision_id: string; state_version: number;
} | null): {
  project_id?: string; branch_id?: string; expected_base_revision_id?: string;
  expected_state_version?: number; idempotency_key?: string;
} {
  if (document) context = { ...context, currentRevisionId: document.head_revision_id, stateVersion: document.state_version };
  if (
    !context.projectId
    || !context.branchId
    || !context.currentRevisionId
  ) {
    return {};
  }
  return {
    project_id: context.projectId,
    branch_id: context.branchId,
    expected_base_revision_id: context.currentRevisionId,
    ...(context.stateVersion !== undefined ? { expected_state_version: context.stateVersion } : {}),
    idempotency_key: createId(),
  };
}

export function shouldApplyDurableEvent(
  lastEventSequence: number,
  event: DurableTaskEvent,
): boolean {
  return event.sequence > lastEventSequence;
}

export function durableSnapshotReplayCursor(
  currentWorkflowRunId: string | null,
  currentCursor: number,
  snapshot: DurableTaskSnapshot,
): number {
  if (currentWorkflowRunId !== snapshot.id) return 0;
  return Math.min(currentCursor, snapshot.last_event_sequence);
}

export function durableSnapshotHeadRevision(
  snapshot: DurableTaskSnapshot,
  fallback: string | null,
): string | null {
  if (!snapshot.change_set) return fallback;
  if (snapshot.change_set.status === "committed") {
    return snapshot.change_set.head_revision_id
      || snapshot.change_set.candidate_revision_id;
  }
  if (["rejected", "changes_requested", "rolled_back"].includes(
    snapshot.change_set.status,
  )) {
    return snapshot.change_set.head_revision_id
      || snapshot.change_set.base_revision_id;
  }
  return fallback && fallback !== snapshot.change_set.base_revision_id
    ? fallback
    : snapshot.change_set.base_revision_id;
}

export function durableResultTaskStatus(
  result: GenerationResult,
  fallback: string | null,
): string | null {
  if (!result.workflow_run_id) return fallback;
  if (result.needs_confirmation) return "waiting_confirmation";
  return result.success ? "succeeded" : "failed";
}

export function durableResultHeadRevision(
  result: GenerationResult,
  fallback: string | null,
): string | null {
  if (result.needs_confirmation) {
    return result.expected_base_revision_id || fallback;
  }
  if (result.change_set_id) {
    // Candidate results do not prove a commit. Keep the head synchronized by
    // the change-set snapshot/action, including after reconnecting post-commit.
    return fallback || result.expected_base_revision_id || null;
  }
  return result.revision_id || fallback;
}

export function durableResultNeedsCommit(
  result: GenerationResult | null | undefined,
  currentRevisionId: string | null | undefined,
  reviewStatus?: string | null,
): boolean {
  if (["committed", "rejected", "changes_requested", "rolled_back"].includes(reviewStatus || "")) return false;
  return Boolean(
    result?.change_set_id
    && result.revision_id
    && result.revision_id !== currentRevisionId
  );
}

export interface DurableChangeSetEventState {
  changeSetId: string;
  status: string;
  baseRevisionId: string | null;
  candidateRevisionId: string | null;
  currentRevisionId: string | null;
  candidateStatus: string | null;
  currentStage: "review" | "complete";
}

export function durableChangeSetEventState(
  event: Pick<DurableTaskEvent, "event_type" | "payload">,
  fallbackCurrentRevisionId: string | null,
): DurableChangeSetEventState | null {
  const statusByEvent: Record<string, string> = {
    "change_set.evidence_updated": "pending_review",
    "change_set.accepted": "accepted",
    "change_set.committed": "committed",
    "change_set.changes_requested": "changes_requested",
    "change_set.rejected": "rejected",
    "change_set.rolled_back": "rolled_back",
  };
  const changeSetId = typeof event.payload.change_set_id === "string"
    ? event.payload.change_set_id
    : "";
  const status = statusByEvent[event.event_type]
    || (typeof event.payload.status === "string" ? event.payload.status : "");
  if (!changeSetId || !status) return null;

  const baseRevisionId = typeof event.payload.base_revision_id === "string"
    ? event.payload.base_revision_id
    : null;
  const candidateRevisionId = typeof event.payload.candidate_revision_id === "string"
    ? event.payload.candidate_revision_id
    : null;
  const terminal = ["changes_requested", "rejected", "rolled_back"]
    .includes(status);
  const currentRevisionId = status === "committed"
    ? candidateRevisionId || fallbackCurrentRevisionId
    : terminal
      ? baseRevisionId || fallbackCurrentRevisionId
      : baseRevisionId || fallbackCurrentRevisionId;
  return {
    changeSetId,
    status,
    baseRevisionId,
    candidateRevisionId,
    currentRevisionId,
    candidateStatus: status === "accepted"
      ? "accepted"
      : status === "pending_review"
        ? "reviewable"
        : null,
    currentStage: status === "committed" || terminal ? "complete" : "review",
  };
}

export function durableChangeSetHeadRevision(
  detail: DurableChangeSetDetail,
): string {
  if (detail.head_revision_id) {
    return detail.head_revision_id;
  }
  return detail.status === "committed"
    ? detail.candidate_revision_id
    : detail.base_revision_id;
}

export function webSocketAuthProtocol(token: string | null | undefined) {
  if (!token) return null;
  const bytes = new TextEncoder().encode(token);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const encoded = btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
  return `cad-agent-auth.${encoded}`;
}

export function durableEventStep(event: DurableTaskEvent): StepUpdate {
  const projected = event.projection;
  const message = projected?.message || (typeof event.payload.message === "string"
    ? event.payload.message
    : event.event_type);
  const failed = event.event_type.includes("failed")
    || event.event_type.includes("timed_out");
  return {
    step: event.event_type,
    message,
    status: projected?.status || (failed ? "failed" : "running"),
    stage_id: projected?.stage || null,
    attempt: projected?.attempt_number || null,
    started_at: event.occurred_at,
    detail: {
      ...event.payload,
      ...(projected || {}),
      source: "durable_task_event",
      sequence: event.sequence,
      workflow_run_id: event.workflow_run_id,
    },
  };
}
