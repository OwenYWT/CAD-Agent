import type { CloudDocument, DocumentRevisionView, DocumentViewIdentity, DocumentViewMode } from "../types/document";

export function viewIdentity(head: CloudDocument, choice?: { mode: DocumentViewMode; revisionId: string; changeSetId?: string | null }): DocumentViewIdentity {
  const revision = choice?.mode === "committed" ? head.head_revision_id : choice?.revisionId || head.head_revision_id;
  return { documentId: head.document_id, viewedRevisionId: revision, headRevisionId: head.head_revision_id,
    headStateVersion: head.state_version, mode: revision === head.head_revision_id ? "committed" : choice?.mode || "history",
    changeSetId: choice?.changeSetId };
}

export function viewLabel(identity: DocumentViewIdentity | null): string {
  if (!identity) return "尚未建立云文档";
  return identity.mode === "committed" ? `已保存修订${identity.viewedRevisionNumber === undefined ? "" : ` #${identity.viewedRevisionNumber}`} · ${identity.viewedRevisionId.slice(0, 8)}`
    : `${identity.mode === "candidate" ? "候选版本 · 未提交" : "历史版本 · 只读"} · ${identity.viewedRevisionId.slice(0, 8)}`;
}

export function engineeringSourceLabel(sourceRevision: string, viewedRevision: string, headRevision: string): string {
  if (sourceRevision !== viewedRevision) return "其他修订，不能验证当前查看模型";
  return viewedRevision === headRevision ? "本次查看的已提交版本" : "本次查看的历史版本";
}

export function projectViewedDocument(head: CloudDocument, identity: DocumentViewIdentity, evidence: DocumentRevisionView | null, connected: boolean): CloudDocument | null {
  if (head.document_id !== identity.documentId) return null;
  if (evidence && (evidence.document_id !== identity.documentId || evidence.revision_id !== identity.viewedRevisionId)) return null;
  if (identity.mode !== "committed" && !evidence) return null;
  return { ...head, ...(identity.mode === "committed" ? {} : evidence || {}), head_revision_id: head.head_revision_id, state_version: head.state_version,
    can_edit: identity.mode === "committed" && connected && head.can_edit,
    can_share: identity.mode === "committed" && head.can_share,
    revision_id: identity.viewedRevisionId, view_mode: identity.mode };
}
