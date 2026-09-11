import type { CloudDocument, DocumentEvent, DocumentDelta, FeatureAnnotationDelta } from "../types/document.ts";

export function applyDocumentEvent(current: CloudDocument, event: DocumentEvent): CloudDocument {
  if (event.sequence <= current.event_sequence) return current;
  if (event.sequence !== current.event_sequence + 1) throw new Error("文档事件不连续，正在重新同步");
  if (event.event_type === "permissions.changed") throw new Error("项目权限已变更，正在重新同步");
  if (event.event_type === "feature.annotated") {
    const annotation = event.payload as FeatureAnnotationDelta;
    const feature = current.features.find((f) => f.id === annotation.feature_id);
    if (!feature || annotation.annotation_version !== (feature.annotation_version || 0) + 1) {
      throw new Error("特征标注版本不连续，正在重新同步");
    }
    return { ...current, event_sequence: event.sequence, features: current.features.map((f) =>
      f.id === annotation.feature_id ? { ...f, role: annotation.role, intent: annotation.intent,
        annotation_version: annotation.annotation_version, annotation_source: annotation.annotation_source } : f) };
  }
  if (event.event_type !== "state_delta") return { ...current, event_sequence: event.sequence };
  const delta = event.payload as DocumentDelta;
  if (delta.base_revision_id !== current.head_revision_id || delta.base_state_version !== current.state_version
      || delta.state_version !== current.state_version + 1) throw new Error("文档版本不连续，正在重新同步");
  const features = new Map(current.features.map((f) => [f.id, f]));
  delta.removed.forEach((id) => features.delete(id));
  delta.upserted.forEach((f) => features.set(f.id, f));
  return { ...current, features: [...features.values()], roots: delta.roots,
    head_revision_id: delta.revision_id, revision_id: delta.revision_id,
    state_version: delta.state_version, event_sequence: event.sequence,
    mesh: delta.mesh, fcstd: delta.fcstd, state: delta.state,
    modeling_backend: delta.modeling_backend, parameter_state_sha256: delta.parameter_state_sha256 };
}
