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

export function durableWriteIdentity(context: DurableWriteContext): {
  project_id?: string; branch_id?: string; expected_base_revision_id?: string;
  expected_state_version?: number; idempotency_key?: string;
} {
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
  return snapshot.change_set.status === "committed"
    ? snapshot.change_set.candidate_revision_id
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

export function durableChangeSetHeadRevision(
  detail: DurableChangeSetDetail,
): string {
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
