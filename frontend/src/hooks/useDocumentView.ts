import { useEffect, useState } from "react";
import type { GenerationResult } from "../types";
import type { CloudDocument, DocumentRevisionView, DocumentViewMode } from "../types/document";
import { getDocumentRevisionView } from "../services/engineeringService";
import { projectViewedDocument, viewIdentity } from "../adapters/documentView";
import { guardDraft } from "../stores/draftGuard";

export function useDocumentView(head: CloudDocument | null, connected: boolean, latest: GenerationResult | null, reviewStatus?: string | null) {
  const [choice, setChoice] = useState<{ documentId: string; mode: DocumentViewMode; revisionId: string; changeSetId?: string | null } | null>(null);
  const [loaded, setLoaded] = useState<{ key: string; evidence?: DocumentRevisionView; error?: string } | null>(null);
  const rejected = reviewStatus === "rejected" || reviewStatus === "rolled_back";
  const chosen = choice && choice.documentId === head?.document_id && !(rejected && choice.mode === "candidate" && choice.revisionId === latest?.revision_id) ? choice : null;
  const firstCandidate = !rejected && !head?.modeling_backend && latest?.success && latest.revision_id && latest.branch_id === head?.document_id
    ? { mode: "candidate" as const, revisionId: latest.revision_id, changeSetId: latest.change_set_id } : undefined;
  const requestedIdentity = head ? viewIdentity(head, chosen || firstCandidate) : null;
  const documentId = requestedIdentity?.documentId, revisionId = requestedIdentity?.viewedRevisionId;
  const key = `${documentId}:${revisionId}`;
  useEffect(() => {
    if (!documentId || !revisionId) return;
    const controller = new AbortController();
    void getDocumentRevisionView(documentId, revisionId, controller.signal).then(evidence => {
      if (evidence.document_id !== documentId || evidence.revision_id !== revisionId) throw new Error("返回的版本身份不匹配");
      if (!controller.signal.aborted) setLoaded({ key, evidence });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setLoaded({ key, error: error instanceof Error ? error.message : "版本读取失败" });
    });
    return () => controller.abort();
  }, [documentId, revisionId, key]);
  const evidence = loaded?.key === key ? loaded.evidence || null : null;
  const identity = requestedIdentity && evidence && requestedIdentity.mode !== "committed" && evidence.review_status
    ? {...requestedIdentity, mode: (["committed", "rolled_back", "rejected"].includes(evidence.review_status) ? "history" : "candidate") as DocumentViewMode}
    : requestedIdentity;
  const document = head && identity ? projectViewedDocument(head, identity, evidence, connected) : null;
  const snapshot = evidence?.snapshot && Object.keys(evidence.snapshot.files || {}).length ? evidence.snapshot : null;
  const result = snapshot ? { ...snapshot.result, snapshot_id: snapshot.id, revision_id: snapshot.revision_id || snapshot.id,
    project_id: snapshot.project_id, branch_id: snapshot.branch_id, version: snapshot.version }
    : latest?.revision_id === revisionId ? latest : null;
  const show = (mode: DocumentViewMode, revision: string, changeSetId?: string | null) => {
    if (!head) return;
    guardDraft(() => setChoice({ documentId: head.document_id, mode, revisionId: revision, changeSetId }));
  };
  return { identity, document, result, evidence, loading: loaded?.key !== key,
    error: loaded?.key === key ? loaded.error : undefined, show,
    showCommitted: () => { if (head) show("committed", head.head_revision_id); } };
}
